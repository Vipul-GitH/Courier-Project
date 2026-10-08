"""Local MySQL mirror for the courier-printing UI.

Remote LabMate, webhook, and home-collection databases are read-only sources.
Manual print approval lives only in the local mirror's `patients` row.
"""

import os
from datetime import datetime
from pathlib import Path

import pymysql

LOCAL_DATABASE = os.getenv("COURIER_LOCAL_DB_NAME", "courier_printing_local")


def load_local_env():
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_local_env()
LOCAL_DATABASE = os.getenv("COURIER_LOCAL_DB_NAME", "courier_printing_local")


def source_connection(database=None):
    return pymysql.connect(
        host=os.getenv("LABMATE_DB_HOST", "10.1.1.44"),
        port=int(os.getenv("LABMATE_DB_PORT", "3306")),
        user=os.getenv("LABMATE_DB_USER", "sahil"),
        password=os.environ["LABMATE_DB_PASSWORD"],
        database=database or os.getenv("LABMATE_DB_NAME", "labmate_fetched_data"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
    )


def local_connection(database=LOCAL_DATABASE):
    return pymysql.connect(
        host=os.getenv("LOCAL_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("LOCAL_MYSQL_PORT", "3306")),
        user=os.getenv("LOCAL_MYSQL_USER", "root"),
        password=os.getenv("LOCAL_MYSQL_PASSWORD", ""),
        database=database,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=60,
        autocommit=False,
    )


def ensure_local_schema():
    with local_connection(database=None) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{LOCAL_DATABASE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()

    statements = [
        """
        CREATE TABLE IF NOT EXISTS patients (
            patient_id BIGINT PRIMARY KEY,
            bdate DATETIME NULL,
            booking_date DATE NULL,
            patient_name VARCHAR(255) NULL,
            mobile_no VARCHAR(50) NULL,
            company VARCHAR(255) NULL,
            delivery_mode VARCHAR(50) NULL,
            urgent_report TINYINT(1) NOT NULL DEFAULT 0,
            net_user_pass VARCHAR(255) NULL,
            charge_as VARCHAR(10) NULL,
            req_slip_no VARCHAR(100) NULL,
            req_slip_date DATETIME NULL,
            charge DECIMAL(12,2) NULL,
            discount DECIMAL(12,2) NULL,
            net DECIMAL(12,2) NULL,
            receipt DECIMAL(12,2) NULL,
            balance DECIMAL(12,2) NULL,
            receipt_no TEXT NULL,
            bill_no TEXT NULL,
            source_synced_at DATETIME NULL,
            local_synced_at DATETIME NOT NULL,
            manual_approved_print TINYINT(1) NOT NULL DEFAULT 0,
            manual_decision_at DATETIME NULL,
            manual_approval_type VARCHAR(30) NULL,
            partial_review_status VARCHAR(20) NULL,
            partial_review_webhook_id BIGINT NULL,
            partial_review_completed_hash CHAR(40) NULL,
            partial_review_at DATETIME NULL,
            webhook_id BIGINT NULL,
            webhook_test_list MEDIUMTEXT NULL,
            webhook_report_path VARCHAR(1000) NULL,
            webhook_report_status VARCHAR(100) NULL,
            webhook_completed_hash CHAR(40) NULL,
            webhook_synced_at DATETIME NULL,
            receipt_cached_path VARCHAR(500) NULL,
            receipt_cached_at DATETIME NULL,
            receipt_cache_error TEXT NULL,
            report_printed TINYINT(1) NOT NULL DEFAULT 0,
            report_printed_at DATETIME NULL,
            INDEX idx_patients_booking_date (booking_date),
            INDEX idx_patients_slip (req_slip_no)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS patient_tests (
            patient_id BIGINT NOT NULL,
            test_code VARCHAR(100) NOT NULL,
            active TINYINT(1) NOT NULL DEFAULT 1,
            source_synced_at DATETIME NULL,
            PRIMARY KEY (patient_id, test_code),
            INDEX idx_patient_tests_patient (patient_id)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS print_attempts (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            batch_id BIGINT UNSIGNED NULL,
            patient_id BIGINT NOT NULL,
            booking_label VARCHAR(120) NULL,
            status VARCHAR(20) NOT NULL,
            stage VARCHAR(20) NOT NULL,
            receipt_included TINYINT(1) NOT NULL DEFAULT 0,
            error_message TEXT NULL,
            attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            INDEX idx_print_attempts_batch (batch_id),
            INDEX idx_print_attempts_patient_time (patient_id, attempted_at),
            INDEX idx_print_attempts_status_time (status, attempted_at)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS print_batches (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            total_patients INT NOT NULL DEFAULT 0,
            completed_patients INT NOT NULL DEFAULT 0,
            failed_patients INT NOT NULL DEFAULT 0,
            error_message TEXT NULL,
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME NULL,
            PRIMARY KEY (id),
            INDEX idx_print_batches_status_time (status, started_at)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS courier_envelopes (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            envelope_id VARCHAR(40) NOT NULL,
            booking_key VARCHAR(160) NOT NULL,
            booking_label VARCHAR(120) NULL,
            booking_date DATE NULL,
            recipient_name VARCHAR(255) NULL,
            recipient_address TEXT NULL,
            mobile_no VARCHAR(50) NULL,
            patient_ids TEXT NOT NULL,
            source_signature VARCHAR(64) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'not_printed',
            printed_at DATETIME NULL,
            printed_by VARCHAR(80) NULL,
            reprint_count INT NOT NULL DEFAULT 0,
            latest_reprint_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_courier_envelopes_envelope_id (envelope_id),
            UNIQUE KEY uq_courier_envelopes_booking_key (booking_key),
            INDEX idx_courier_envelopes_status_date (status, booking_date)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS voided_courier_envelopes (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            envelope_id VARCHAR(40) NOT NULL,
            booking_key VARCHAR(160) NULL,
            booking_label VARCHAR(120) NULL,
            replaced_by_envelope_id VARCHAR(40) NULL,
            reason VARCHAR(120) NULL,
            voided_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_voided_courier_envelope_id (envelope_id),
            INDEX idx_voided_courier_booking (booking_key)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS label_scan_verifications (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            scan_batch_id VARCHAR(40) NOT NULL,
            envelope_id VARCHAR(40) NOT NULL,
            booking_key VARCHAR(160) NULL,
            booking_label VARCHAR(120) NULL,
            scanned_by_user_id VARCHAR(80) NULL,
            scanned_by_name VARCHAR(150) NULL,
            scanned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(20) NOT NULL,
            raw_batch_text MEDIUMTEXT NULL,
            PRIMARY KEY (id),
            UNIQUE KEY uq_label_scan_envelope (envelope_id),
            INDEX idx_label_scan_batch (scan_batch_id),
            INDEX idx_label_scan_status_time (status, scanned_at)
        ) ENGINE=InnoDB
        """,
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            setting_key VARCHAR(100) NOT NULL,
            setting_value TEXT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (setting_key)
        ) ENGINE=InnoDB
        """,
    ]
    with local_connection() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            # CREATE TABLE does not add new fields to an already-created local
            # mirror, so keep lightweight migrations here.
            cur.execute("SHOW COLUMNS FROM patients LIKE 'report_printed'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN report_printed TINYINT(1) NOT NULL DEFAULT 0"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'report_printed_at'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN report_printed_at DATETIME NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'manual_approval_type'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN manual_approval_type VARCHAR(30) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'partial_review_status'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN partial_review_status VARCHAR(20) NULL"
                )
            cur.execute("SHOW COLUMNS FROM courier_envelopes LIKE 'source_signature'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE courier_envelopes ADD COLUMN source_signature VARCHAR(64) NULL AFTER patient_ids"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'partial_review_webhook_id'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN partial_review_webhook_id BIGINT NULL"
                )
            cur.execute(
                "SHOW COLUMNS FROM patients LIKE 'partial_review_completed_hash'"
            )
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN partial_review_completed_hash CHAR(40) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'partial_review_at'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN partial_review_at DATETIME NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_id'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE patients ADD COLUMN webhook_id BIGINT NULL")
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_test_list'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN webhook_test_list MEDIUMTEXT NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_report_path'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN webhook_report_path VARCHAR(1000) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_report_status'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN webhook_report_status VARCHAR(100) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_completed_hash'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN webhook_completed_hash CHAR(40) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'webhook_synced_at'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN webhook_synced_at DATETIME NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'receipt_cached_path'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN receipt_cached_path VARCHAR(500) NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'receipt_cached_at'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN receipt_cached_at DATETIME NULL"
                )
            cur.execute("SHOW COLUMNS FROM patients LIKE 'receipt_cache_error'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE patients ADD COLUMN receipt_cache_error TEXT NULL"
                )
            cur.execute("SHOW COLUMNS FROM print_attempts LIKE 'batch_id'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE print_attempts ADD COLUMN batch_id BIGINT UNSIGNED NULL, ADD INDEX idx_print_attempts_batch (batch_id)"
                )
            cur.execute("SHOW COLUMNS FROM print_attempts LIKE 'booking_label'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE print_attempts ADD COLUMN booking_label VARCHAR(120) NULL"
                )
        conn.commit()


def chunks(values, size=500):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def sync_booking_date(booking_date):
    """Read one booking date's Courier/By Hand patients and upsert into local MySQL."""
    ensure_local_schema()
    now = datetime.now()
    with source_connection() as source:
        with source.cursor() as cur:
            cur.execute(
                """
                SELECT patient_id, bdate, booking_date, patient_name, mobile_no, company,
                       delivery_mode, urgent_report, net_user_pass, charge_as, req_slip_no,
                       req_slip_date, charge, discount, net, receipt, balance, receipt_no,
                       bill_no, synced_at
                FROM patients
                WHERE booking_date = %s
                  AND (
                      FIND_IN_SET('C', REPLACE(COALESCE(delivery_mode, ''), ' ', ''))
                      OR FIND_IN_SET('H', REPLACE(COALESCE(delivery_mode, ''), ' ', ''))
                  )
                """,
                [booking_date],
            )
            patients = cur.fetchall()
            patient_ids = [row["patient_id"] for row in patients]

            tests = []
            if patient_ids:
                for patient_chunk in chunks(patient_ids):
                    placeholders = ", ".join(["%s"] * len(patient_chunk))
                    cur.execute(
                        f"SELECT patient_id, test_code, active, synced_at FROM patient_tests WHERE patient_id IN ({placeholders})",
                        patient_chunk,
                    )
                    tests.extend(cur.fetchall())

    with local_connection() as local:
        with local.cursor() as cur:
            if patients:
                cur.executemany(
                    """
                    INSERT INTO patients (
                        patient_id,bdate,booking_date,patient_name,mobile_no,company,delivery_mode,
                        urgent_report,net_user_pass,charge_as,req_slip_no,req_slip_date,charge,
                        discount,net,receipt,balance,receipt_no,bill_no,source_synced_at,local_synced_at
                    ) VALUES (
                        %(patient_id)s,%(bdate)s,%(booking_date)s,%(patient_name)s,%(mobile_no)s,%(company)s,%(delivery_mode)s,
                        %(urgent_report)s,%(net_user_pass)s,%(charge_as)s,%(req_slip_no)s,%(req_slip_date)s,%(charge)s,
                        %(discount)s,%(net)s,%(receipt)s,%(balance)s,%(receipt_no)s,%(bill_no)s,%(synced_at)s,%(local_synced_at)s
                    ) ON DUPLICATE KEY UPDATE
                        bdate=VALUES(bdate), booking_date=VALUES(booking_date), patient_name=VALUES(patient_name),
                        mobile_no=VALUES(mobile_no), company=VALUES(company), delivery_mode=VALUES(delivery_mode),
                        urgent_report=VALUES(urgent_report), net_user_pass=VALUES(net_user_pass), charge_as=VALUES(charge_as),
                        req_slip_no=VALUES(req_slip_no), req_slip_date=VALUES(req_slip_date), charge=VALUES(charge),
                        discount=VALUES(discount), net=VALUES(net), receipt=VALUES(receipt), balance=VALUES(balance),
                        receipt_no=VALUES(receipt_no), bill_no=VALUES(bill_no), source_synced_at=VALUES(source_synced_at),
                        local_synced_at=VALUES(local_synced_at)
                    """,
                    [{**row, "local_synced_at": now} for row in patients],
                )
            if tests:
                cur.executemany(
                    """
                    INSERT INTO patient_tests (patient_id,test_code,active,source_synced_at)
                    VALUES (%(patient_id)s,%(test_code)s,%(active)s,%(synced_at)s)
                    ON DUPLICATE KEY UPDATE active=VALUES(active), source_synced_at=VALUES(source_synced_at)
                    """,
                    tests,
                )
        local.commit()

    return {"patients": len(patients), "patient_tests": len(tests)}
