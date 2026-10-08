# Chrome-Teal-Contacts-RPA

Teal has no bulk import for contacts. This tool takes your spreadsheet (one row per
contact, e.g. a Teal contact export) and builds each contact in Teal completely,
then checks Teal against the spreadsheet. The spreadsheet is the source of truth.

It works in two passes:

**Pass 1 - LinkedIn (this is where it asks you things).** For each contact with a
blank Email or Phone, it opens their LinkedIn **Contact info**
(`<profile>/overlay/contact-info/`):

- one email / one phone found: used as-is;
- several phone numbers (or emails): it lists them and you pick one;
- neither an email nor a phone found anywhere: it asks you to type them (Enter skips).

Everything found or typed is written into the spreadsheet. Then it pauses so you
can look the spreadsheet over before anything goes into Teal.

**Pass 2 - Teal (runs on its own).** For each contact it:

1. Finds the contact in Teal if it's already there (by LinkedIn URL) and updates it,
   or creates it with **+ Add a New Contact** (name, title, company, email,
   LinkedIn, phone).
2. Writes Teal's ID for the contact (the part after `/contact-tracker/` in the
   address bar) into the spreadsheet's `teal_contact_id` column right away, so a
   re-run always reopens the same contact and never makes a duplicate.
3. Opens the contact and sets **Relationship**, **Goal**, **Status** and
   **Follow up** (and **Last contacted** if your sheet has it). Teal saves these by
   itself; there's no Save button.
4. Reloads the contact and checks every field against the spreadsheet: name,
   title/company, email, phone, LinkedIn, the dropdowns and the dates. Anything
   that differs is fixed (dropdowns/dates on the page, the rest through **Edit**)
   and checked again.
5. Writes the result to the spreadsheet's `rpa_status` column: `done` when
   everything matches, or `needs attention` with the details in `rpa_notes`. It
   only stops to ask you when something still doesn't match after fixing.

Formatting differences don't count as mismatches: `(203) 555-0100` matches
`203-555-0100`, `Co-Worker` matches Teal's `Co-worker`, `10/16/2026` matches
`2026-10-16`. Blank spreadsheet cells are left alone in Teal.

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
# See what would happen (and spot problems in the sheet); doesn't open Chrome
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx" --check

# Walk through everything without saving anything
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx" --dry-run

# First real try: one contact
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx" --limit 1

# Everything that's left
python teal_contacts.py "C:\Users\benew\OneDrive\AI Projects\Chrome Teal Contact RPA\Uploads\Upload 1.xlsx"
```

**Close the spreadsheet in Excel while the tool runs**, so it can write into it
(at the pause between the passes you can open it, look, and close it again; the
Teal pass re-reads it, so your edits count). If the file is locked (open in Excel,
or OneDrive still syncing), the tool retries briefly, then keeps the values in its
progress file and writes them on a later run.

When a contact still doesn't match after the tool's fixes, it shows what differs:
**Enter** leaves it marked `needs attention`, **r** lets the tool try again,
**c** re-checks after you've fixed it yourself in Chrome. **Ctrl+C** stops at any
point; a re-run carries on where it left off.

| Option | What it does |
|---|---|
| `--check` | Validate the spreadsheet and list pending contacts; no browser |
| `--dry-run` | Do both passes without saving anything in Teal or the spreadsheet; lists what would change |
| `--limit N` | Process at most N pending contacts this run |
| `--linkedin-only` | Only pass 1: find emails/phones and write them to the sheet |
| `--teal-only` | Only pass 2: don't visit LinkedIn |
| `--yes` | Don't pause between the passes |
| `--unattended` | Never pause in pass 2; mismatches are just marked `needs attention` |
| `--review` | Pause before saving each new contact so you can look at the form |
| `--reset` | Forget all progress and start over (doesn't touch Teal or the sheet) |
| `--sheet NAME` | Use this worksheet instead of the first one |
| `--delay SECONDS` | Pause between LinkedIn lookups (default 4, plus up to 50% random) |
| `--retries N` | Retries when a Teal page or form doesn't load (default 2, with backoff) |
| `--timeout SECONDS` | How long to wait for Teal's and LinkedIn's pages (default 20) |
| `--chrome-path PATH` | Location of `chrome.exe` if it isn't found automatically |
| `--profile-dir PATH` | Use a different Chrome profile folder for the tool |

### What it changes in your spreadsheet

- **Email Address / Phone**: filled in from LinkedIn or what you type (never
  overwritten if already filled).
- New columns, added at the end the first time: **teal_contact_id**,
  **rpa_status** (`done`, `needs attention`, `failed`, `skipped`) and **rpa_notes**
  (what didn't match, or why it failed).

### What it creates next to your spreadsheet

- `Upload 1.xlsx.status.json`: progress per contact (status, reason, Teal ID,
  email/phone found, whether LinkedIn was already checked). Re-runs skip `done`
  contacts and retry the rest. Delete it, or use `--reset`, to start over.
- `logs\teal_contacts-<date>-<time>.log`: everything each run did.
- `failures\row-<N>.png`: a screenshot of Chrome when a row fails or needs attention.
- `failures\linkedin-row-<N>.txt` / `.png`: what LinkedIn showed when no email or
  phone was found.

At the end of a run you get a summary: what LinkedIn found, and how many contacts
are done, need attention, failed or were skipped (with reasons).

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
| Email | `Email Address`, `Email` | no (LinkedIn / asked if blank) |
| Phone | `Phone`, `Phone Number` | no (LinkedIn / asked if blank) |
| Relationship | `contact_relationship_type`, `Relationship` | no |
| Goal | `contact_intention_type`, `Goal` | no |
| Status | `contact_next_step_type`, `Status` | no |
| Follow up | `follow_up_at`, `Follow up` | no |
| Last contacted | `last_contacted_at`, `Last contacted` | no |
| Location | `Location` | no |
| Twitter | `twitter_handle`, `Twitter` | no |

- **Dropdown values** can be Teal's export format (`{"name":"Mentor","id":"7"}`)
  or plain text (`Mentor`). They must be one of Teal's options (case doesn't
  matter): Relationship: Self, Co-worker, Friend, Family, Other, Recruiter, Mentor,
  Hiring manager, Alumni. Goal: Networking, Informational interview, Request
  referral, Research interviewer, Research career. Status: To be contacted, Follow
  up needed, Meeting scheduled, Thank you sent. `--check` flags anything else.
- **Dates** can be Excel dates, `2026-10-16` or `10/16/2026`.
- **Repeated columns** (Teal's export has the three dropdown columns twice): the
  first non-empty value is used; if the copies disagree the row gets a note.

Rows with a missing or non-LinkedIn URL, or the same URL as an earlier row, are
listed and skipped. See `examples/sample_contacts.csv`.

## Troubleshooting

**"Teal opened ... instead of the contact tracker"**: you're signed out of Teal in
the tool's Chrome window. Sign in there and press Enter in the terminal.

**"Chrome started but didn't open its debugging port"**: a Chrome window using
the tool's profile is open from before (e.g. you opened it yourself). Close those
Chrome windows and run again. Your normal Chrome windows are fine to leave open.

**"LinkedIn wants you to sign in"**: sign in to LinkedIn in the tool's Chrome
window and press Enter in the terminal.

**"LinkedIn: no email or phone shared on LinkedIn"**: usually normal; the person
hasn't shared them with you, so the tool asks you instead. If you know they do
share an email, look at `failures\linkedin-row-<N>.txt` and `.png`: they show
exactly what the tool saw in LinkedIn's Contact info box.

**LinkedIn shows the profile instead of the Contact info box**: LinkedIn sometimes
opens `.../overlay/contact-info/` as the plain profile page. The tool then clicks
the profile's own **Contact info** link to open the box. It also checks that the
box shows this person's profile link, so it never takes details from someone else.

**"LinkedIn's Contact info box didn't appear (the tab shows ...)"**: neither the
link nor the click opened the box: LinkedIn was slow, showed a security check, or
changed its page. `failures\linkedin-row-<N>.txt` / `.png` show what the tab had. The tool asks you for the email/phone instead
and tries LinkedIn again on the next run if you skip. If it happens every time, open one `.../overlay/contact-info/` link in
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

**"Can't write to Upload 1.xlsx - is it open in Excel (or still syncing)?"**: close
it in Excel. The values are kept in the `.status.json` file and written on the next run.

**"needs attention: Status: Teal shows ..., sheet has ..."**: the tool set the field
but Teal shows something else after reloading. Fix it in Teal (or the sheet) and
re-run; the contact is reopened by its ID, not created again.

**"... isn't one of Teal's options"**: the spreadsheet has a dropdown value Teal
doesn't offer (a typo, or Teal renamed an option). Fix the cell and re-run. If Teal
added or renamed options, also update `CHOICE_OPTIONS` in `teal_rpa/teal.py` (only
used by `--check`).

**"Saved, but couldn't find the new contact in Teal's list"**: the contact was
created but the tool couldn't work out its ID. Open it in Teal, copy the ID from the
address bar (after `/contact-tracker/`) into the row's `teal_contact_id` cell and
re-run.

**"Couldn't find day N in the calendar" / "Couldn't set Follow up"**: the Follow up
or Last contacted calendar didn't behave as expected. The calendar's HTML is saved
as `failures\calendar-follow_up-<number>.html` (with a screenshot `row-<N>.png`);
send those along when reporting it.

**"Teal contact <ID> didn't open"**: the contact was deleted in Teal, or the
`teal_contact_id` cell is wrong. Clear the cell (and `--reset` if needed) to create
it again.

**Duplicate contacts in Teal**: before creating a contact the tool looks for one
with the same LinkedIn URL (or the same name with no LinkedIn saved) and updates
that instead. A contact saved in Teal with a different LinkedIn URL is treated as a
different person.

**Teal changed its page**: the labels the tool looks for are at the top of
`teal_rpa/teal.py` (button names, form placeholders, section and field labels).

## Development

```powershell
pip install -r requirements-dev.txt
pytest
```

The end-to-end tests drive Chrome (headless) against local stand-ins of Teal's
contact tracker (`tests/fake_teal/`) and LinkedIn's Contact info overlay. Set `TEAL_RPA_TEST_CHROME` to a Chrome or
Chromium path if it isn't found automatically.
