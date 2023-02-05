## USPS Passport Appointment Watcher

### Usage

```
$ python watcher.py --help
Usage: watcher.py [OPTIONS]

Options:
  --zip TEXT                     ZIP code.
  --city-and-state TEXT          City and state (e.g., Austin, TX).
  --radius INTEGER               Radius to search for locations, in miles.
  --interval INTEGER             Interval in seconds between processing each
                                 date.
  --num-adults INTEGER           Number of adults for appointment.
  --num-minors INTEGER           Number of minors for appointment.
  --appointment-type [PASSPORT]
  --discord-webhook TEXT         Discord webhook URL to send notifications to.
  --help                         Show this message and exit.
```
