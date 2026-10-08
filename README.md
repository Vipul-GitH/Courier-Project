# ARPRA Courier Printing dashboard

Flask + Jinja dashboard that reads the local `labmate_fetched_data` database and shows only today's patients whose `delivery_mode` contains:

- `C` — Courier
- `H` — By Hand

Combined delivery modes are supported, so `C,F` and `C,F,H` are included.

## Run

The app automatically loads the local `.env` file. For a different database, copy `.env.example` to `.env` and fill in its values. You can also override the values from PowerShell:

```powershell
$env:LABMATE_DB_HOST = "10.1.1.44"
$env:LABMATE_DB_PORT = "3306"
$env:LABMATE_DB_NAME = "labmate_fetched_data"
$env:LABMATE_DB_USER = "sahil"
$env:LABMATE_DB_PASSWORD = "your-password"
python app.py
```

Open `http://127.0.0.1:5012` on this PC. For another device on the same LAN,
open `http://YOUR-PC-LAN-IP:5012` (for example `http://192.168.1.25:5012`).
The application only executes `SELECT` queries.
