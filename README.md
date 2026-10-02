# Chrome-Teal-Contacts-RPA

Teal has no bulk import for contacts. This tool reads your spreadsheet and, for each
row, opens Teal's **Contact Tracker** (`app.tealhq.com/contact-tracker`), clicks
**+ Add a New Contact**, fills in the form from the spreadsheet and clicks
**Save Contact**. The spreadsheet is the source of truth.

For each contact it:

1. If **Email** or **Phone** is blank in the spreadsheet, opens the person's
   LinkedIn **Contact info** (`<profile>/overlay/contact-info/`) and uses the email
   and phone shown there. What it finds is written back into the spreadsheet.
   LinkedIn only shows these when the person shares them with you (mostly
   1st-degree connections), so blanks are normal. With `--ask-missing` it then
   asks you in the terminal for anything still blank (Enter skips).
2. Fills First Name, Last Name, Job Title, Company Name, Email, LinkedIn, Phone
   (plus Location and Twitter if your sheet has them) and checks each value stuck.
3. Pauses so you can look at the form in Chrome, then saves it when you press Enter.
   Use `--auto-save` to skip the pause once you trust it.
4. Records the result, so the next run skips contacts that are already done.

## One-time setup (Windows)

### 1. Install Python and Git

Open **PowerShell** (Start menu, type "PowerShell") and run:

```powershell
winget install Python.Python.3.12
winget install Git.Git
```

Close PowerShell and open a new one so it picks up the new programs. Check:

```powershell
py --version
git --version
```

### 2. Get the code from GitHub

This downloads the project into `C:\Users\benew\Chrome-Teal-Contacts-RPA`. Git
remembers where it came from, so later updates are one command (`git pull`).

```powershell
cd $HOME
git clone https://github.com/benewm/Chrome-Teal-Contacts-RPA.git
cd Chrome-Teal-Contacts-RPA
git checkout claude/linkedin-contacts-rpa-a0108k
```

If GitHub asks you to sign in, a browser window opens; sign in as usual.

To get the latest version later: `cd C:\Users\benew\Chrome-Teal-Contacts-RPA`
then `git pull`.

### 3. Install the tool's Python packages

From the project folder:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell says running scripts is disabled, run this once and try again:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

You don't need `playwright install`: the tool uses your normal Google Chrome.

**Every time you open a new PowerShell to use the tool**, first run:

```powershell
cd C:\Users\benew\Chrome-Teal-Contacts-RPA
.venv\Scripts\Activate.ps1
```

(`(.venv)` at the start of the prompt means it's active.)

### 4. Sign in to Teal and LinkedIn in the tool's Chrome window (first run only)

Chrome doesn't allow automation on your everyday profile (since Chrome 136), so the
tool runs Chrome with its own profile folder:
`%LOCALAPPDATA%\TealContactsRPA\chrome-profile`.

The first time you run the tool, a new Chrome window opens on Teal's sign-in page
and the terminal says so. Sign in to Teal there (Google sign-in works), then press
Enter in the terminal. The first time it needs LinkedIn, the same happens for
LinkedIn: sign in to LinkedIn in that window, then press Enter. Chrome remembers both
sign-ins, so later runs go straight to work. You don't need the Teal extension in
this profile.

## Running it

Your spreadsheet can live anywhere; pass its full path (tip: in File Explorer,
Shift + right-click the file, **Copy as path**, then paste).

```powershell
# See what would happen; doesn't open Chrome
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx" --check

# First real try: one contact
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx" --limit 1

# Everything that's left, reviewing each before it saves
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx"
```

At the review pause: **Enter** saves, **s** skips this contact (it's retried next
run), **q** stops the run (this contact stays pending). You can also correct a
field in Chrome before pressing Enter. **Ctrl+C** stops at any point.

**Close the spreadsheet in Excel while the tool runs**, so it can write the
emails/phones it finds (or you type) back into it. If it's open, the tool tells you and keeps
the values in its progress file instead.

| Option | What it does |
|---|---|
| `--check` | Validate the spreadsheet and list pending contacts; no browser |
| `--limit N` | Process at most N pending contacts this run |
| `--auto-save` | Save without the review pause |
| `--no-linkedin` | Don't look up blank Email/Phone on LinkedIn |
| `--ask-missing` | Ask in the terminal for Email/Phone still blank after LinkedIn |
| `--dry-run` | Do everything except Save: fill each form, then click Cancel. Records nothing |
| `--reset` | Forget all progress and start over (doesn't touch Teal) |
| `--sheet NAME` | Use this worksheet instead of the first one |
| `--delay SECONDS` | Pause between contacts (default 4, plus up to 50% random) |
| `--retries N` | Retries when Teal's page or form doesn't load (default 2, with backoff) |
| `--timeout SECONDS` | How long to wait for Teal's page and form (default 20) |
| `--chrome-path PATH` | Location of `chrome.exe` if it isn't found automatically |
| `--profile-dir PATH` | Use a different Chrome profile folder for the tool |

### What it creates next to your spreadsheet

- `contacts.xlsx.status.json`: progress (done / failed / skipped per contact, with
  the reason, any email/phone found or typed, and whether LinkedIn was already
  checked, so a retried row doesn't visit LinkedIn again). Delete it, or use `--reset`, to
  start over.
- `logs\teal_contacts-<date>-<time>.log`: everything each run did.
- `failures\row-<N>.png`: a screenshot of Chrome whenever a row fails.

At the end of a run you get a summary: how many succeeded, failed (with reasons)
and were skipped.

## Spreadsheet format

`.xlsx` or `.csv`, headers in row 1, one contact per row. Teal's own contact export
layout works as-is. Headers are matched case-insensitively:

| Field | Accepted headers | Required |
|---|---|---|
| LinkedIn URL | `URL`, `LinkedIn URL`, `Link`, `Profile URL` | yes |
| First name | `First Name` | yes |
| Last name | `Last Name` | yes |
| Job title | `Position`, `Title`, `Job Title` | yes |
| Company | `Company` | yes |
| Email | `Email Address`, `Email` | no (looked up on LinkedIn if blank) |
| Phone | `Phone`, `Phone Number` | no (looked up on LinkedIn if blank) |
| Location | `Location` | no |
| Twitter | `twitter_handle`, `Twitter` | no |

Example row:

| First Name | Last Name | Email Address | Phone | Position | Company | URL |
|---|---|---|---|---|---|---|
| Jane | Example | jane@example.com | 555-010-1234 | VP Engineering | Acme Corp | https://www.linkedin.com/in/jane-example |

Other columns are ignored for now (they're for a later version that fills in
relationship, goal, status and follow-up after the contact is saved). Rows with a
missing or non-LinkedIn URL, or the same URL as an earlier row, are listed and
skipped. See `examples/sample_contacts.csv`.

## Troubleshooting

**"Teal opened ... instead of the contact tracker"**: you're signed out of Teal in
the tool's Chrome window. Sign in there and press Enter in the terminal.

**"Chrome started but didn't open its debugging port"**: a Chrome window using
the tool's profile is open from before (e.g. you opened it yourself). Close those
Chrome windows and run again. Your normal Chrome windows are fine to leave open.

**"LinkedIn wants you to sign in"**: sign in to LinkedIn in the tool's Chrome
window and press Enter in the terminal.

**"LinkedIn: no email or phone shared on LinkedIn"**: usually normal; the person
hasn't shared them with you. The contact is still created. Use `--ask-missing` to
type them in yourself. If you know they do share an email, look at
`failures\linkedin-row-<N>.txt` and `.png`: they show exactly what the tool saw in
LinkedIn's Contact info box.

**"LinkedIn's Contact info box didn't appear"**: LinkedIn was slow, showed a
security check, or changed its page. The contact is still created without
email/phone. If it happens every time, open one `.../overlay/contact-info/` link in
the tool's Chrome window to see what LinkedIn shows.

**Go easy on LinkedIn**: LinkedIn limits how many profiles you can view and may
show security checks if you view many quickly. Keep runs to about 30-50 contacts
(`--limit 40`) and keep the default delay.

**"Couldn't find Google Chrome"**: pass `--chrome-path "C:\Program Files\Google\Chrome\Application\chrome.exe"`.

**"The Add a New Contact form didn't appear" / "The 'Add a New Contact' button
didn't appear"**: Teal was slow or changed its page. Check the screenshot in
`failures\`. Try `--timeout 40`. If Teal renamed the button, update `ADD_BUTTON`
in `teal_rpa/teal.py`.

**"Couldn't fill the 'Job Title' box" (or another field)**: Teal changed that
field's placeholder text (the grey hint inside the empty box). Update it in
`FIELD_PLACEHOLDERS` in `teal_rpa/teal.py` to match what Teal now shows.

**"Clicked Save Contact but the form stayed open"**: Teal rejected something
(the reason is included when Teal shows one, e.g. an invalid email). The contact
may or may not have been created, so check Teal before re-running that row; fix
the spreadsheet cell, then re-run (failed rows are retried automatically).

**"Can't write to contacts.xlsx - is it open in Excel?"**: close it in Excel. The
values you typed are kept in the `.status.json` file and used on the next run.

**Duplicate contacts in Teal**: the tool only knows what it did itself. If you
delete or `--reset` the `.status.json` file, contacts already in Teal will be added
again.

## Development

```powershell
pip install -r requirements-dev.txt
pytest
```

The end-to-end tests drive Chrome (headless) against local stand-ins of Teal's
contact tracker (`tests/fake_teal/`) and LinkedIn's Contact info overlay. Set `TEAL_RPA_TEST_CHROME` to a Chrome or
Chromium path if it isn't found automatically.
