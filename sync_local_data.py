"""Run a one-date remote-to-local courier data sync."""

import argparse
from datetime import date

from local_store import sync_booking_date

parser = argparse.ArgumentParser()
parser.add_argument(
    "--date", default=date.today().isoformat(), help="Booking date: YYYY-MM-DD"
)
args = parser.parse_args()
print(sync_booking_date(args.date))
