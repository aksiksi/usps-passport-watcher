## USPS Passport Appointment Watcher

### Install Dependencies

```
# Using Pip
pip install -r requirements.txt

# Using Pipenv
pipenv shell
```

### Usage

```
Usage: watcher.py [OPTIONS]

Options:
  --zip-code TEXT                ZIP code.
  --city-and-state TEXT          City and state (e.g., Austin, TX).
  --radius INTEGER               Radius to search for locations, in miles.
                                 [default: 10]
  --interval INTEGER             Interval in seconds between processing each
                                 date.  [default: 5]
  --num-adults INTEGER           Number of adults for appointment.  [default:
                                 1]
  --num-minors INTEGER           Number of minors for appointment.  [default:
                                 0]
  --appointment-type [PASSPORT]  [default: PASSPORT]
  --start-date TEXT              Format: YYYYMMDD.
  --end-date TEXT                Format: YYYYMMDD.
  --schedule / --no-schedule     If set, automatically schedule an
                                 appointment.
  --name TEXT                    Name for the appoinment.
  --email TEXT                   Email for the appoinment.
  --phone TEXT                   Phone number for the appointment (format:
                                 444-555-6666).
  --discord-webhook TEXT         Discord webhook URL to send notifications to.
  --help                         Show this message and exit.
```
