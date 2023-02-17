import asyncio
import datetime
import logging
from typing import Optional, Set

import aiohttp
import backoff
import click

MAX_RETRY_BACKOFF_TIME = 30  # seconds
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36 Edg/109.0.1518.78"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Content-Type": "application/json;charset=utf-8",
    "x-requested-with": "XMLHttpRequest",
    "origin": "https://tools.usps.com",
    "referer": "https://tools.usps.com/rcas.htm",
}
VALID_APPOINTMENT_TYPES = ["PASSPORT"]
RETRIABLE_HTTP_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientResponseError,
    aiohttp.ContentTypeError,
)
NUM_CONCURRENT_DATES = 5

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


@backoff.on_exception(
    backoff.expo,
    RETRIABLE_HTTP_ERRORS,
    max_time=MAX_RETRY_BACKOFF_TIME,
)
async def send_discord_webhook(
    session: aiohttp.ClientSession,
    webhook_url: str,
    content: str,
):
    payload = {
        "content": content,
    }
    async with session.post(webhook_url, json=payload) as resp:
        resp.raise_for_status()


class AppointmentWatcher:
    APPOINTMENT_TIME_SEARCH_URL = (
        "https://tools.usps.com/UspsToolsRestServices/rest/v2/appointmentTimeSearch"
    )
    FACILITY_SCHEDULE_SEARCH_URL = (
        "https://tools.usps.com/UspsToolsRestServices/rest/v2/facilityScheduleSearch"
    )
    CREATE_APPOINTMENT_URL = (
        "https://tools.usps.com/UspsToolsRestServices/rest/v2/createAppointment"
    )

    def __init__(
        self,
        zip_code: Optional[str],
        city: Optional[str],
        state: Optional[str],
        radius: int = 10,
        interval: int = 3,  # seconds
        num_adults: int = 1,
        num_minors: int = 0,
        appointment_type: str = "PASSPORT",
        schedule: bool = False,
        name: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
        discord_webhook: Optional[str] = None,
    ) -> None:
        if zip_code is None and city is None and state is None:
            raise ValueError("One of ZIP or city/state must be specified.")
        elif zip_code is None and (city is None or state is None):
            raise ValueError("Both city and state must be specified.")

        if schedule and (name is None or email is None or phone is None):
            raise ValueError(
                "Name, email, and phone must be provided to schedule an appointment."
            )

        self.zip_code = zip_code
        self.city = city
        self.state = state
        self.radius = radius
        self.interval = interval
        self.num_adults = num_adults
        self.num_minors = num_minors
        self.appointment_type = appointment_type.upper()
        self.schedule = schedule
        self.name = name
        self.email = email
        self.phone = phone
        self.discord_webhook = discord_webhook

        # Keeps track of what appointments we've found to avoid sending
        # duplicate notifications.
        self.appointments_found: Set[str] = set()

    @backoff.on_exception(
        backoff.expo,
        RETRIABLE_HTTP_ERRORS,
        max_time=MAX_RETRY_BACKOFF_TIME,
    )
    async def list_facility_schedules(
        self,
        session: aiohttp.ClientSession,
        date: datetime.date,
    ) -> list[dict[str, any]]:
        payload = {
            "date": date.strftime("%Y%m%d"),
            "city": self.city or "",
            "state": self.state or "",
            "zip5": self.zip_code or "",
            "radius": f"{self.radius}",
            "poScheduleType": self.appointment_type,
            "numberOfAdults": f"{self.num_adults}",
            "numberOfMinors": f"{self.num_minors}",
        }
        async with session.post(
            self.FACILITY_SCHEDULE_SEARCH_URL, json=payload
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body["facilityDetails"]

    @backoff.on_exception(
        backoff.expo,
        RETRIABLE_HTTP_ERRORS,
        max_time=MAX_RETRY_BACKOFF_TIME,
    )
    async def list_times_for_facility(
        self,
        session: aiohttp.ClientSession,
        date: datetime.date,
        fdbID: int,
    ) -> list[dict[str, any]]:
        payload = {
            "date": date.strftime("%Y%m%d"),
            "fdbId": [f"{fdbID}"],
            "productType": self.appointment_type,
            "numberOfAdults": f"{self.num_adults}",
            "numberOfMinors": f"{self.num_minors}",
            "skipEndOfDayRecord": True,
        }
        async with session.post(self.APPOINTMENT_TIME_SEARCH_URL, json=payload) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body["appointmentTimeDetailExtended"]

    # @backoff.on_exception(
    #     backoff.expo,
    #     RETRIABLE_HTTP_ERRORS,
    #     max_time=MAX_RETRY_BACKOFF_TIME,
    # )
    async def create_appointment(
        self,
        session: aiohttp.ClientSession,
        appt_time: datetime.datetime,
        fdbId: int,
    ) -> str:
        # NOTE(aksiksi): This isn't working, likely due to missing cookies.
        # Returns 405 (method not allowed).
        first_name, last_name = self.name.split(" ")
        area_code, exchange, line = self.phone.split("-")
        passport_photo_idx = 0 if self.appointment_type == "PASSPORT" else 1
        payload = {
            "customer": {
                "firstName": f"{first_name.upper()}",
                "lastName": f"{last_name.upper()}",
                "regId": "",
            },
            "customerEmailAddress": f"{self.email.upper()}",
            "customerPhone": {
                "areaCode": f"{area_code}",
                "exchange": f"{exchange}",
                "line": f"{line}",
                "textable": False,
            },
            "fdbId": f"{fdbId}",
            "date": appt_time.strftime("%Y%m%d"),
            "time": appt_time.strftime("%I:%M %p").lower(),
            "schedulingType": self.appointment_type,
            "serviceCenter": "Web Service Center",
            "numberOfAdults": f"{self.num_adults}",
            "numberOfMinors": f"{self.num_minors}",
            "passportPhotoIndicator": passport_photo_idx,
            "ipAddress": "8.8.8.8",
        }
        async with session.post(
            self.CREATE_APPOINTMENT_URL, json=payload, headers=HEADERS
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body["scheduling"]["confirmationNumber"]

    async def handle_appointment(
        self,
        session: aiohttp.ClientSession,
        date: datetime.date,
        appt_time: Optional[datetime.datetime],
        location: str,
    ):
        if not appt_time:
            if self.zip_code:
                logger.warning(
                    f"No {self.appointment_type} appointments found on {date} within {self.radius} miles of ZIP {self.zip_code}"
                )
            else:
                logger.warning(
                    f"No {self.appointment_type} appointments found on {date} within {self.radius} miles of {self.city}, {self.state}"
                )
            return

        # Have we seen this appointment before?
        appt_string = f"{appt_time}-{location}"
        if appt_string in self.appointments_found:
            logger.warning(f"Skipping notification for appointment: {appt_string}")
            return

        if self.schedule:
            confirmation_number = await self.create_appointment(
                session, appt_time, facility_id
            )
            confirmation_url = f"https://tools.usps.com/rcas-confirmation.htm?confirmationNumber={confirmation_number}"
            message = f"Found & scheduled passport appointment on {appt_time} at {location}. Visit {confirmation_url} to manage your appointment."
        else:
            message = f"Found passport appointment on {appt_time} at {location}; schedule it here: https://tools.usps.com/rcas.htm."

        logger.warning(message)

        if self.discord_webhook:
            await send_discord_webhook(session, self.discord_webhook, message)

        self.appointments_found.add(appt_string)

    async def run_for_date(self, date: datetime.date):
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            # Find a facility with an empty slot on the date.
            schedules = await self.list_facility_schedules(session, date)
            facility_id: Optional[int] = None
            for facility in schedules:
                for date_info in facility["date"]:
                    result_date = datetime.datetime.strptime(
                        date_info["date"], "%Y%m%d"
                    ).date()
                    if result_date == date and date_info["status"]:
                        facility_id = facility["fdbId"]
                        break
            if facility_id is None:
                return None, date

            facility_addr = facility["address"]
            location = f"{facility_addr['addressLineOne']}, {facility_addr['city']}, {facility_addr['postalCode']}"

            # Check appointment times for the facility on the given date.
            appt_time: Optional[datetime.datetime] = None
            for time_info in await self.list_times_for_facility(
                session, date, facility_id
            ):
                # Yes, this is how they return available time slots...
                is_slot_free = time_info[
                    "appointmentStatus"
                ].lower() == "available" and (
                    "color" not in time_info or time_info["color"] != "gray"
                )
                if is_slot_free:
                    appt_time = datetime.datetime.fromisoformat(
                        time_info["startDateTime"]
                    )
                    break

            await self.handle_appointment(session, date, appt_time, location)

    def get_valid_dates(
        self,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
    ) -> list[datetime.date]:
        min_date = datetime.date.today() + datetime.timedelta(days=1)
        max_date = min_date + datetime.timedelta(days=30)
        if start_date:
            if start_date < min_date:
                logger.warning(
                    f"Start date must be greater than {min_date}; setting to that..."
                )
            else:
                min_date = start_date
        if end_date:
            if end_date > max_date:
                logger.warning(
                    f"End date must be less than {max_date}; setting to that..."
                )
            else:
                max_date = end_date

        valid_dates = []
        d = min_date
        while d <= max_date:
            valid_dates.append(d)
            d += datetime.timedelta(days=1)

        return valid_dates

    async def run(
        self,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
    ) -> Optional[datetime.datetime]:
        while True:
            valid_dates = self.get_valid_dates(start_date, end_date)

            # Group the valid dates into chunks that will be processed concurrently..
            chunk_iter = (
                valid_dates[i : i + NUM_CONCURRENT_DATES]
                for i in range(0, len(valid_dates), NUM_CONCURRENT_DATES)
            )

            for chunk in chunk_iter:
                tasks = [self.run_for_date(d) for d in chunk]
                await asyncio.gather(*tasks)

            await asyncio.sleep(self.interval)


@click.command()
@click.option("--zip-code", type=str, help="ZIP code.")
@click.option("--city-and-state", type=str, help="City and state (e.g., Austin, TX).")
@click.option(
    "--radius",
    default=10,
    type=int,
    help="Radius to search for locations, in miles.",
    show_default=True,
)
@click.option(
    "--interval",
    default=5,
    type=int,
    help="Interval in seconds between processing each date.",
    show_default=True,
)
@click.option(
    "--num-adults",
    default=1,
    type=int,
    help="Number of adults for appointment.",
    show_default=True,
)
@click.option(
    "--num-minors",
    default=0,
    type=int,
    help="Number of minors for appointment.",
    show_default=True,
)
@click.option(
    "--appointment-type",
    type=click.Choice(VALID_APPOINTMENT_TYPES, case_sensitive=False),
    default="PASSPORT",
    show_default=True,
)
@click.option(
    "--start-date",
    type=str,
    help="Format: YYYYMMDD.",
)
@click.option(
    "--end-date",
    type=str,
    help="Format: YYYYMMDD.",
)
@click.option(
    "--schedule/--no-schedule",
    default=False,
    type=bool,
    help="If set, automatically schedule an appointment.",
)
@click.option(
    "--name",
    type=str,
    help="Name for the appoinment.",
)
@click.option(
    "--email",
    type=str,
    help="Email for the appoinment.",
)
@click.option(
    "--phone",
    type=str,
    help="Phone number for the appointment (format: 444-555-6666).",
)
@click.option(
    "--discord-webhook", type=str, help="Discord webhook URL to send notifications to."
)
def watcher(
    zip_code: Optional[str],
    city_and_state: Optional[str],
    radius: int,
    interval: int,
    num_adults: int,
    num_minors: int,
    appointment_type: str,
    start_date: Optional[str],
    end_date: Optional[str],
    schedule: bool,
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    discord_webhook: Optional[str],
):
    if zip_code is None and city_and_state is None:
        click.echo("One of --zip-code or --city-and-state must be set.", err=True)
        return
    if zip_code is not None and city_and_state is not None:
        click.echo("Only one of --zip-code or --city-and-state can be set.", err=True)
        return

    city, state = None, None
    if city_and_state is not None:
        city, state = city_and_state.split(",")
        city, state = city.strip(), state.strip()

    if start_date:
        start_date = datetime.datetime.strptime(start_date, "%Y%m%d").date()
    if end_date:
        end_date = datetime.datetime.strptime(end_date, "%Y%m%d").date()

    aw = AppointmentWatcher(
        zip_code=zip_code,
        city=city,
        state=state,
        radius=radius,
        interval=interval,
        num_adults=num_adults,
        num_minors=num_minors,
        appointment_type=appointment_type,
        schedule=schedule,
        name=name,
        email=email,
        phone=phone,
        discord_webhook=discord_webhook,
    )
    asyncio.run(aw.run(start_date=start_date, end_date=end_date))


if __name__ == "__main__":
    watcher()
