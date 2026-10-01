# Chrome-Teal-Contacts-RPA

Creates contacts in [Teal](https://www.tealhq.com/) from a spreadsheet of LinkedIn
contacts, by filling in the Teal - Job Search Companion extension's
"Add Contact Manually" form for each row. The spreadsheet is the source of truth.

> Work in progress: spreadsheet loading and run tracking are done; the Chrome/Teal
> automation is next. Full setup and troubleshooting docs come with it.

## Spreadsheet

`.xlsx` or `.csv`, header in row 1, one contact per row. Teal's own contact export
layout works as-is. Recognised headers (case-insensitive):

| Field        | Accepted headers                          | Required |
|--------------|-------------------------------------------|----------|
| LinkedIn URL | `URL`, `LinkedIn URL`, `Link`, `Profile URL` | yes   |
| First name   | `First Name`                              | yes      |
| Last name    | `Last Name`                               | yes      |
| Job title    | `Position`, `Title`, `Job Title`          | yes      |
| Company      | `Company`                                 | yes      |
| Email        | `Email Address`, `Email`                  | no       |
| Phone        | `Phone`, `Phone Number`                   | no       |

Other columns are kept but ignored. See `examples/sample_contacts.csv`.

Progress is saved next to the spreadsheet as `<file>.status.json`, so re-runs skip
contacts already done. `--reset` clears it.

## Quick start (Windows)

```powershell
winget install Python.Python.3.12        # once; then open a new PowerShell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python teal_contacts.py C:\path\to\contacts.xlsx
```

Run the tests with `pip install -r requirements-dev.txt` then `pytest`.
