import asyncio
import datetime
import logging
from typing import Optional

import aiohttp
import backoff
import click

MAX_RETRY_BACKOFF_TIME = 30  # seconds
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36 Edg/109.0.1518.78"
VALID_APPOINTMENT_TYPES = ["PASSPORT"]
RETRIABLE_HTTP_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientResponseError,
    aiohttp.ContentTypeError,
)

logger = logging.getLogger(__name__)


def get_next_date(date: datetime.date, max_lookahead: int = 30) -> datetime.date:
    today = datetime.date.today()
    next_date = date + datetime.timedelta(days=1)
    if next_date > today + datetime.timedelta(days=max_lookahead):
        return today
    return next_date


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

    def __init__(
        self,
        zip: Optional[str],
        city: Optional[str],
        state: Optional[str],
        radius: int = 10,
        interval: int = 3,  # seconds
        num_adults: int = 1,
        num_minors: int = 0,
        appointment_type: str = "PASSPORT",
        discord_webhook: Optional[str] = None,
    ) -> None:
        if zip is None and city is None and state is None:
            raise ValueError("One of ZIP or city/state must be specified.")
        elif zip is None and (city is None or state is None):
            raise ValueError("Both city and state must be specified.")

        self.zip = zip
        self.city = city
        self.state = state
        self.radius = radius
        self.interval = interval
        self.num_adults = num_adults
        self.num_minors = num_minors
        self.appointment_type = appointment_type.upper()
        self.discord_webhook = discord_webhook

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
            "zip5": self.zip or "",
            "radius": f"{self.radius}",
            "poScheduleType": f"{self.appointment_type}",
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
            "productType": f"{self.appointment_type}",
            "numberOfAdults": f"{self.num_adults}",
            "numberOfMinors": f"{self.num_minors}",
            "skipEndOfDayRecord": True,
        }
        async with session.post(self.APPOINTMENT_TIME_SEARCH_URL, json=payload) as resp:
            resp.raise_for_status()
            body = await resp.json()
            return body["appointmentTimeDetailExtended"]

    async def run_for_date(self, date: datetime.date) -> Optional[datetime.datetime]:
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
            # Find a facility with an empty slot on the date (+-1 day).
            schedules = await self.list_facility_schedules(session, date)
            facility_id: int = None
            for facility in schedules:
                # Check if the date (+-1 day) is available.
                for date_info in facility["date"]:
                    if date_info["status"]:
                        facility_id = facility["fdbId"]
                        break
            if facility_id is None:
                return None

            # Check appointment times for all 3 dates.
            appt_time: Optional[datetime.datetime] = None
            for d in [
                date - datetime.timedelta(days=1),
                date,
                date + datetime.timedelta(days=1),
            ]:
                for time_info in await self.list_times_for_facility(
                    session, d, facility_id
                ):
                    if time_info["selectable"]:
                        appt_time = datetime.datetime.fromisoformat(
                            time_info["startDateTime"]
                        )

            if appt_time is not None and self.discord_webhook is not None:
                message = f"Found passport appointment at {appt_time}; schedule it here: https://tools.usps.com/rcas.htm"
                await send_discord_webhook(session, self.discord_webhook, message)

            return appt_time

    async def run(
        self,
        date: Optional[datetime.date] = None,
    ) -> Optional[datetime.datetime]:
        if date is None:
            date = datetime.date.today()

        while True:
            appt_time = await self.run_for_date(date)
            if appt_time is not None:
                return appt_time

            if self.zip:
                logger.warning(
                    f"No {self.appointment_type} appointments found on {date} within {self.radius} miles of ZIP {self.zip}"
                )
            else:
                logger.warning(
                    f"No {self.appointment_type} appointments found on {date} within {self.radius} miles of {self.city}, {self.state}"
                )

            date = get_next_date(date)

            await asyncio.sleep(self.interval)


@click.command()
@click.option("--zip", type=str, help="ZIP code.")
@click.option("--city-and-state", type=str, help="City and state (e.g., Austin, TX).")
@click.option(
    "--radius",
    default=10,
    type=int,
    help="Radius to search for locations, in miles.",
)
@click.option(
    "--interval",
    default=3,
    type=int,
    help="Interval in seconds between processing each date.",
)
@click.option(
    "--num-adults",
    default=1,
    type=int,
    help="Number of adults for appointment.",
)
@click.option(
    "--num-minors",
    default=0,
    type=int,
    help="Number of minors for appointment.",
)
@click.option(
    "--appointment-type",
    type=click.Choice(VALID_APPOINTMENT_TYPES, case_sensitive=False),
    default="PASSPORT",
)
@click.option(
    "--discord-webhook", type=str, help="Discord webhook URL to send notifications to."
)
def watcher(
    zip: Optional[str],
    city_and_state: Optional[str],
    radius: int,
    interval: int,
    num_adults: int,
    num_minors: int,
    appointment_type: str,
    discord_webhook: Optional[str],
):
    if zip is None and city_and_state is None:
        click.echo("One of --zip or --city-and-state must be set.", err=True)
        return
    if zip is not None and city_and_state is not None:
        click.echo("Only one of --zip or --city-and-state can be set.", err=True)
        return

    city, state = None, None
    if city_and_state is not None:
        city, state = city_and_state.split(",")
        city, state = city.strip(), state.strip()

    aw = AppointmentWatcher(
        zip=zip,
        city=city,
        state=state,
        radius=radius,
        interval=interval,
        num_adults=num_adults,
        num_minors=num_minors,
        appointment_type=appointment_type,
        discord_webhook=discord_webhook,
    )
    asyncio.run(aw.run())


if __name__ == "__main__":
    watcher()
