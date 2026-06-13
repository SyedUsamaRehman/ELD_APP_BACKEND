"""
ELD Engine - HOS (Hours of Service) Calculator
Property-carrying driver, 70hrs/8days cycle
"""
from dataclasses import dataclass, field
from typing import List, Tuple
from math import ceil

# HOS Constants - Property Carrier 70hr/8day
DRIVE_LIMIT_DAILY = 11.0        # Max driving hours per shift
ON_DUTY_LIMIT_DAILY = 14.0      # Max on-duty window per shift
BREAK_REQUIRED_AFTER = 8.0      # Hours before mandatory 30-min break
BREAK_DURATION = 0.5            # 30-minute break
RESET_REST = 10.0               # Hours off-duty required to reset
CYCLE_LIMIT = 70.0              # 70 hours in 8 days
AVG_SPEED_MPH = 55.0            # Average driving speed
FUEL_INTERVAL_MILES = 1000.0    # Fuel stop every 1000 miles
FUEL_STOP_DURATION = 0.5        # 30 minutes for fueling
PICKUP_DROPOFF_DURATION = 1.0   # 1 hour each for pickup/dropoff


@dataclass
class Stop:
    name: str
    stop_type: str          # 'start', 'pickup', 'dropoff', 'rest', 'break', 'fuel', 'sleeper'
    location: str
    arrival_time: float     # hours from trip start
    departure_time: float
    duration: float
    odometer: float         # miles from start
    lat: float = 0.0
    lon: float = 0.0
    day: int = 1


@dataclass
class LogEntry:
    """Represents one row in an ELD log - status changes"""
    time: float         # hour of day (0-24)
    status: str         # 'off_duty', 'sleeper', 'driving', 'on_duty_not_driving'
    location: str
    remarks: str = ""


@dataclass
class DailyLog:
    day_number: int
    date_label: str
    log_entries: List[LogEntry] = field(default_factory=list)
    from_location: str = ""
    to_location: str = ""
    total_miles: float = 0.0
    carrier: str = "Independent Carrier"
    driver_name: str = "Driver"
    truck_number: str = "TRK-001"
    trailer_number: str = "TRL-001"

    # Hour totals
    hours_off_duty: float = 0.0
    hours_sleeper: float = 0.0
    hours_driving: float = 0.0
    hours_on_duty: float = 0.0


@dataclass
class TripResult:
    total_distance_miles: float
    total_duration_hours: float
    stops: List[Stop]
    daily_logs: List[DailyLog]
    total_days: int
    route_waypoints: List[Tuple[float, float]]  # (lat, lon)
    summary: dict


def calculate_trip(
    current_location: str,
    pickup_location: str,
    dropoff_location: str,
    cycle_used_hours: float,
    geocode_func,
    route_func
) -> TripResult:
    """
    Main trip calculation function.
    Uses geocode_func(location) -> (lat, lon)
    Uses route_func(origin_coords, dest_coords) -> (distance_miles, waypoints)
    """

    # Geocode all locations
    current_coords = geocode_func(current_location)
    pickup_coords = geocode_func(pickup_location)
    dropoff_coords = geocode_func(dropoff_location)

    # Get route segments
    seg1_dist, seg1_wp = route_func(current_coords, pickup_coords)    # to pickup
    seg2_dist, seg2_wp = route_func(pickup_coords, dropoff_coords)    # to dropoff

    total_distance = seg1_dist + seg2_dist
    all_waypoints = seg1_wp + seg2_wp[1:]  # merge, avoid duplicate midpoint

    # Calculate fuel stop positions
    fuel_stops_miles = _get_fuel_stop_positions(total_distance)

    # Now simulate the drive with HOS rules
    stops, daily_logs = _simulate_hos(
        current_location=current_location,
        pickup_location=pickup_location,
        dropoff_location=dropoff_location,
        current_coords=current_coords,
        pickup_coords=pickup_coords,
        dropoff_coords=dropoff_coords,
        seg1_dist=seg1_dist,
        seg2_dist=seg2_dist,
        all_waypoints=all_waypoints,
        cycle_used_hours=cycle_used_hours,
        fuel_stops_miles=fuel_stops_miles
    )

    total_duration = stops[-1].arrival_time if stops else 0
    total_days = max(s.day for s in stops) if stops else 1

    summary = {
        "total_distance_miles": round(total_distance, 1),
        "total_distance_km": round(total_distance * 1.609, 1),
        "total_driving_hours": round(total_distance / AVG_SPEED_MPH, 1),
        "total_trip_hours": round(total_duration, 1),
        "total_days": total_days,
        "num_rest_stops": sum(1 for s in stops if s.stop_type in ('rest', 'sleeper')),
        "num_fuel_stops": sum(1 for s in stops if s.stop_type == 'fuel'),
        "num_breaks": sum(1 for s in stops if s.stop_type == 'break'),
        "cycle_hours_remaining": round(max(0, CYCLE_LIMIT - cycle_used_hours - total_distance / AVG_SPEED_MPH), 1),
    }

    return TripResult(
        total_distance_miles=total_distance,
        total_duration_hours=total_duration,
        stops=stops,
        daily_logs=daily_logs,
        total_days=total_days,
        route_waypoints=all_waypoints,
        summary=summary
    )


def _get_fuel_stop_positions(total_miles: float) -> List[float]:
    """Returns mile markers where fuel stops are needed"""
    positions = []
    pos = FUEL_INTERVAL_MILES
    while pos < total_miles:
        positions.append(pos)
        pos += FUEL_INTERVAL_MILES
    return positions


def _simulate_hos(
    current_location, pickup_location, dropoff_location,
    current_coords, pickup_coords, dropoff_coords,
    seg1_dist, seg2_dist, all_waypoints,
    cycle_used_hours, fuel_stops_miles
) -> Tuple[List[Stop], List[DailyLog]]:
    """
    Simulate HOS compliance with full 70hr/8day rules.
    Returns list of stops and daily log sheets.
    """

    stops = []
    # Track state
    clock = 0.0             # hours elapsed since trip start
    odometer = 0.0          # miles driven
    cycle_hours = cycle_used_hours  # rolling 70hr used
    shift_drive = 0.0       # hours driven this shift (resets after 10hr off)
    shift_on_duty = 0.0     # hours on-duty this shift
    drive_since_break = 0.0 # hours driven since last 30-min break
    day = 1
    day_start_clock = 0.0   # when the current day started (midnight boundary)

    def get_day(c):
        return int(c // 24) + 1

    def get_time_of_day(c):
        return c % 24

    # Add start stop
    stops.append(Stop(
        name="Start - " + current_location,
        stop_type="start",
        location=current_location,
        arrival_time=0.0,
        departure_time=0.0,
        duration=0.0,
        odometer=0.0,
        lat=current_coords[0],
        lon=current_coords[1],
        day=1
    ))

    # Remaining distance to drive
    remaining_seg1 = seg1_dist
    remaining_seg2 = seg2_dist
    total_remaining = seg1_dist + seg2_dist
    fuel_stops_set = set(round(m) for m in fuel_stops_miles)
    fuel_stops_remaining = sorted(fuel_stops_miles)

    # Track daily log entries per day
    # day -> list of (clock_time, status, location, remarks)
    log_data = {}  # day -> list of dicts

    def add_log(day_n, clock_t, status, location, remarks=""):
        if day_n not in log_data:
            log_data[day_n] = []
        log_data[day_n].append({
            "time": get_time_of_day(clock_t),
            "status": status,
            "location": location,
            "remarks": remarks
        })

    # Initial log - start of trip
    add_log(1, 0.0, "on_duty_not_driving", current_location, "Start of trip - pre-trip inspection")

    # Drive a segment, respecting HOS rules
    # Phase: 0 = driving to pickup, 1 = at pickup, 2 = driving to dropoff, 3 = at dropoff
    phase = 0
    trip_complete = False

    max_iterations = 500
    iteration = 0

    def get_location_at_odometer(odo):
        """Approximate location name based on odometer"""
        frac = odo / (seg1_dist + seg2_dist) if (seg1_dist + seg2_dist) > 0 else 0
        if frac < 0.5:
            return f"En route to {pickup_location}"
        else:
            return f"En route to {dropoff_location}"

    while not trip_complete and iteration < max_iterations:
        iteration += 1

        # Check if we need a mandatory 30-min break (8hrs driving without break)
        if drive_since_break >= BREAK_REQUIRED_AFTER and phase in (0, 2):
            # Must take 30-min break
            loc = get_location_at_odometer(odometer)
            stops.append(Stop(
                name=f"Mandatory 30-min Break",
                stop_type="break",
                location=loc,
                arrival_time=clock,
                departure_time=clock + BREAK_DURATION,
                duration=BREAK_DURATION,
                odometer=odometer,
                lat=_interpolate_lat(all_waypoints, odometer, seg1_dist + seg2_dist),
                lon=_interpolate_lon(all_waypoints, odometer, seg1_dist + seg2_dist),
                day=get_day(clock)
            ))
            add_log(get_day(clock), clock, "off_duty", loc, "Mandatory 30-min break")
            clock += BREAK_DURATION
            shift_on_duty += BREAK_DURATION
            add_log(get_day(clock), clock, "on_duty_not_driving", loc, "Break ended - resuming")
            drive_since_break = 0.0
            continue

        # Check cycle limit - need 34-hr reset if at 70hr
        if cycle_hours >= CYCLE_LIMIT - 0.5:
            loc = get_location_at_odometer(odometer)
            rest_hours = 34.0  # 34-hour restart
            stops.append(Stop(
                name="34-Hour Restart (Cycle Reset)",
                stop_type="sleeper",
                location=loc,
                arrival_time=clock,
                departure_time=clock + rest_hours,
                duration=rest_hours,
                odometer=odometer,
                lat=_interpolate_lat(all_waypoints, odometer, seg1_dist + seg2_dist),
                lon=_interpolate_lon(all_waypoints, odometer, seg1_dist + seg2_dist),
                day=get_day(clock)
            ))
            add_log(get_day(clock), clock, "sleeper", loc, "34-hour restart - cycle reset")
            clock += rest_hours
            add_log(get_day(clock), clock, "off_duty", loc, "End of 34-hr restart")
            cycle_hours = 0.0
            shift_drive = 0.0
            shift_on_duty = 0.0
            drive_since_break = 0.0
            continue

        # Check daily shift limits: 11hr drive / 14hr window
        if shift_drive >= DRIVE_LIMIT_DAILY or shift_on_duty >= ON_DUTY_LIMIT_DAILY:
            # Must take 10hr off-duty rest
            loc = get_location_at_odometer(odometer)
            stops.append(Stop(
                name="10-Hour Rest Break",
                stop_type="rest",
                location=loc,
                arrival_time=clock,
                departure_time=clock + RESET_REST,
                duration=RESET_REST,
                odometer=odometer,
                lat=_interpolate_lat(all_waypoints, odometer, seg1_dist + seg2_dist),
                lon=_interpolate_lon(all_waypoints, odometer, seg1_dist + seg2_dist),
                day=get_day(clock)
            ))
            add_log(get_day(clock), clock, "sleeper", loc, "10-hour mandatory rest")
            clock += RESET_REST
            add_log(get_day(clock), clock, "off_duty", loc, "End of rest - resuming shift")
            shift_drive = 0.0
            shift_on_duty = 0.0
            drive_since_break = 0.0
            continue

        if phase == 0:
            # Driving to pickup
            if remaining_seg1 <= 0:
                phase = 1
                continue

            # Check for fuel stop
            next_fuel = fuel_stops_remaining[0] if fuel_stops_remaining else float('inf')
            dist_to_fuel = next_fuel - odometer if next_fuel <= (seg1_dist + seg2_dist) else float('inf')

            # How far can we drive right now?
            max_drive_time = min(
                DRIVE_LIMIT_DAILY - shift_drive,
                ON_DUTY_LIMIT_DAILY - shift_on_duty,
                BREAK_REQUIRED_AFTER - drive_since_break,
                CYCLE_LIMIT - cycle_hours
            )
            max_drive_dist = max_drive_time * AVG_SPEED_MPH

            # How far to next waypoint (pickup, fuel, or segment end)?
            drive_dist = min(remaining_seg1, max_drive_dist)
            if dist_to_fuel < remaining_seg1 and dist_to_fuel <= max_drive_dist:
                drive_dist = dist_to_fuel

            if drive_dist <= 0:
                # Force rest
                shift_drive = DRIVE_LIMIT_DAILY
                continue

            drive_time = drive_dist / AVG_SPEED_MPH

            # Add driving log
            start_day = get_day(clock)
            add_log(start_day, clock, "driving", get_location_at_odometer(odometer), "Driving to pickup")

            clock += drive_time
            odometer += drive_dist
            remaining_seg1 -= drive_dist
            shift_drive += drive_time
            shift_on_duty += drive_time
            drive_since_break += drive_time
            cycle_hours += drive_time

            # Fuel stop?
            if fuel_stops_remaining and odometer >= fuel_stops_remaining[0] - 0.5:
                fs_mile = fuel_stops_remaining.pop(0)
                loc = get_location_at_odometer(odometer)
                stops.append(Stop(
                    name=f"Fuel Stop ({round(odometer)} mi)",
                    stop_type="fuel",
                    location=loc,
                    arrival_time=clock,
                    departure_time=clock + FUEL_STOP_DURATION,
                    duration=FUEL_STOP_DURATION,
                    odometer=odometer,
                    lat=_interpolate_lat(all_waypoints, odometer, seg1_dist + seg2_dist),
                    lon=_interpolate_lon(all_waypoints, odometer, seg1_dist + seg2_dist),
                    day=get_day(clock)
                ))
                add_log(get_day(clock), clock, "on_duty_not_driving", loc, f"Fuel stop at {round(odometer)} miles")
                clock += FUEL_STOP_DURATION
                shift_on_duty += FUEL_STOP_DURATION
                add_log(get_day(clock), clock, "on_duty_not_driving", loc, "Departing fuel stop")

        elif phase == 1:
            # At pickup
            stops.append(Stop(
                name=f"Pickup - {pickup_location}",
                stop_type="pickup",
                location=pickup_location,
                arrival_time=clock,
                departure_time=clock + PICKUP_DROPOFF_DURATION,
                duration=PICKUP_DROPOFF_DURATION,
                odometer=odometer,
                lat=pickup_coords[0],
                lon=pickup_coords[1],
                day=get_day(clock)
            ))
            add_log(get_day(clock), clock, "on_duty_not_driving", pickup_location, "Arrived at pickup - loading")
            clock += PICKUP_DROPOFF_DURATION
            shift_on_duty += PICKUP_DROPOFF_DURATION
            cycle_hours += PICKUP_DROPOFF_DURATION
            add_log(get_day(clock), clock, "on_duty_not_driving", pickup_location, "Loaded - departing pickup")
            phase = 2

        elif phase == 2:
            # Driving to dropoff
            if remaining_seg2 <= 0:
                phase = 3
                continue

            # Check for fuel stop
            next_fuel = fuel_stops_remaining[0] if fuel_stops_remaining else float('inf')
            dist_to_fuel = next_fuel - odometer if next_fuel <= (seg1_dist + seg2_dist) else float('inf')

            max_drive_time = min(
                DRIVE_LIMIT_DAILY - shift_drive,
                ON_DUTY_LIMIT_DAILY - shift_on_duty,
                BREAK_REQUIRED_AFTER - drive_since_break,
                CYCLE_LIMIT - cycle_hours
            )
            max_drive_dist = max_drive_time * AVG_SPEED_MPH

            drive_dist = min(remaining_seg2, max_drive_dist)
            if dist_to_fuel < remaining_seg2 and dist_to_fuel <= max_drive_dist:
                drive_dist = dist_to_fuel

            if drive_dist <= 0:
                shift_drive = DRIVE_LIMIT_DAILY
                continue

            drive_time = drive_dist / AVG_SPEED_MPH

            add_log(get_day(clock), clock, "driving", get_location_at_odometer(odometer), "Driving to dropoff")

            clock += drive_time
            odometer += drive_dist
            remaining_seg2 -= drive_dist
            shift_drive += drive_time
            shift_on_duty += drive_time
            drive_since_break += drive_time
            cycle_hours += drive_time

            # Fuel stop?
            if fuel_stops_remaining and odometer >= fuel_stops_remaining[0] - 0.5:
                fs_mile = fuel_stops_remaining.pop(0)
                loc = get_location_at_odometer(odometer)
                stops.append(Stop(
                    name=f"Fuel Stop ({round(odometer)} mi)",
                    stop_type="fuel",
                    location=loc,
                    arrival_time=clock,
                    departure_time=clock + FUEL_STOP_DURATION,
                    duration=FUEL_STOP_DURATION,
                    odometer=odometer,
                    lat=_interpolate_lat(all_waypoints, odometer, seg1_dist + seg2_dist),
                    lon=_interpolate_lon(all_waypoints, odometer, seg1_dist + seg2_dist),
                    day=get_day(clock)
                ))
                add_log(get_day(clock), clock, "on_duty_not_driving", loc, f"Fuel stop at {round(odometer)} miles")
                clock += FUEL_STOP_DURATION
                shift_on_duty += FUEL_STOP_DURATION
                add_log(get_day(clock), clock, "on_duty_not_driving", loc, "Departing fuel stop")

        elif phase == 3:
            # Arrived at dropoff
            stops.append(Stop(
                name=f"Dropoff - {dropoff_location}",
                stop_type="dropoff",
                location=dropoff_location,
                arrival_time=clock,
                departure_time=clock + PICKUP_DROPOFF_DURATION,
                duration=PICKUP_DROPOFF_DURATION,
                odometer=odometer,
                lat=dropoff_coords[0],
                lon=dropoff_coords[1],
                day=get_day(clock)
            ))
            add_log(get_day(clock), clock, "on_duty_not_driving", dropoff_location, "Arrived at dropoff - unloading")
            clock += PICKUP_DROPOFF_DURATION
            add_log(get_day(clock), clock, "off_duty", dropoff_location, "Trip complete - end of duty")
            trip_complete = True

    # Build daily logs
    daily_logs = _build_daily_logs(log_data, stops, seg1_dist, seg2_dist)

    return stops, daily_logs


def _interpolate_lat(waypoints, odometer, total_dist):
    if not waypoints or total_dist == 0:
        return 0.0
    frac = min(1.0, odometer / total_dist)
    idx = frac * (len(waypoints) - 1)
    i = int(idx)
    if i >= len(waypoints) - 1:
        return waypoints[-1][0]
    t = idx - i
    return waypoints[i][0] * (1 - t) + waypoints[i + 1][0] * t


def _interpolate_lon(waypoints, odometer, total_dist):
    if not waypoints or total_dist == 0:
        return 0.0
    frac = min(1.0, odometer / total_dist)
    idx = frac * (len(waypoints) - 1)
    i = int(idx)
    if i >= len(waypoints) - 1:
        return waypoints[-1][1]
    t = idx - i
    return waypoints[i][1] * (1 - t) + waypoints[i + 1][1] * t


def _build_daily_logs(log_data, stops, seg1_dist, seg2_dist) -> List[DailyLog]:
    """Build DailyLog objects from raw log data"""
    daily_logs = []

    if not log_data:
        return daily_logs

    all_days = sorted(log_data.keys())

    for day_num in all_days:
        entries_raw = sorted(log_data[day_num], key=lambda x: x['time'])

        entries = [LogEntry(
            time=e['time'],
            status=e['status'],
            location=e['location'],
            remarks=e['remarks']
        ) for e in entries_raw]

        # Calculate hour totals for the day
        hours = {"off_duty": 0.0, "sleeper": 0.0, "driving": 0.0, "on_duty_not_driving": 0.0}

        for i, entry in enumerate(entries):
            if i < len(entries) - 1:
                duration = entries[i + 1].time - entry.time
                if duration < 0:
                    duration += 24  # crossed midnight
            else:
                duration = 24.0 - entry.time  # till end of day

            duration = max(0.0, duration)
            if entry.status in hours:
                hours[entry.status] += duration

        # Find from/to locations for this day
        day_stops = [s for s in stops if s.day == day_num]
        from_loc = day_stops[0].location if day_stops else ""
        to_loc = day_stops[-1].location if day_stops else ""

        # Miles for this day
        day_miles = 0.0
        for i in range(len(entries)):
            if entries[i].status == "driving":
                drive_hrs = 0.0
                if i < len(entries) - 1:
                    t = entries[i + 1].time - entries[i].time
                    if t < 0:
                        t += 24
                    drive_hrs = t
                day_miles += drive_hrs * AVG_SPEED_MPH

        dl = DailyLog(
            day_number=day_num,
            date_label=f"Day {day_num}",
            log_entries=entries,
            from_location=from_loc,
            to_location=to_loc,
            total_miles=round(day_miles, 1),
            hours_off_duty=round(hours["off_duty"], 2),
            hours_sleeper=round(hours["sleeper"], 2),
            hours_driving=round(hours["driving"], 2),
            hours_on_duty=round(hours["on_duty_not_driving"], 2),
        )
        daily_logs.append(dl)

    return daily_logs
