import json
import requests
import math
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .eld_engine import calculate_trip, TripResult, Stop, DailyLog, LogEntry


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
HEADERS = {"User-Agent": "ELD-Trip-Planner/1.0 (assessment-app)"}


def geocode(location: str):
    try:
        resp = requests.get(NOMINATIM_URL, params={
            "q": location,
            "format": "json",
            "limit": 1,
        }, headers=HEADERS, timeout=10)
        data = resp.json()
        if data:
            return (float(data[0]["lat"]), float(data[0]["lon"]))
    except Exception as e:
        print(f"Geocode error for {location}: {e}")
    return (39.5, -98.35)


def get_route(origin_coords, dest_coords):
    try:
        orig_str = f"{origin_coords[1]},{origin_coords[0]}"
        dest_str = f"{dest_coords[1]},{dest_coords[0]}"
        url = f"{OSRM_URL}/{orig_str};{dest_str}"
        resp = requests.get(url, params={"overview": "full", "geometries": "geojson"}, headers=HEADERS, timeout=15)
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            dist_miles = route["distance"] * 0.000621371
            coords = route["geometry"]["coordinates"]
            step = max(1, len(coords) // 200)
            waypoints = [(c[1], c[0]) for c in coords[::step]]
            if not waypoints or waypoints[0] != origin_coords:
                waypoints.insert(0, origin_coords)
            if waypoints[-1] != dest_coords:
                waypoints.append(dest_coords)
            return dist_miles, waypoints
    except Exception as e:
        print(f"OSRM error: {e}")
    dist_miles = _haversine_miles(origin_coords, dest_coords)
    return dist_miles, [origin_coords, dest_coords]


def _haversine_miles(c1, c2):
    R = 3958.8
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def _format_time(hours_from_start: float) -> str:
    day = int(hours_from_start // 24) + 1
    h = int(hours_from_start % 24)
    m = int((hours_from_start % 1) * 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"Day {day}, {h12:02d}:{m:02d} {ampm}"


def _format_time_of_day(hour_of_day: float) -> str:
    h = int(hour_of_day) % 24
    m = int((hour_of_day % 1) * 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12:02d}:{m:02d} {ampm}"


def _format_duration(hours: float) -> str:
    h, m = int(hours), int((hours % 1) * 60)
    if h == 0: return f"{m}m"
    if m == 0: return f"{h}h"
    return f"{h}h {m}m"


def _stop_to_dict(stop: Stop) -> dict:
    return {
        "name": stop.name, "stop_type": stop.stop_type, "location": stop.location,
        "arrival_time": round(stop.arrival_time, 2), "departure_time": round(stop.departure_time, 2),
        "duration": round(stop.duration, 2), "odometer": round(stop.odometer, 1),
        "lat": stop.lat, "lon": stop.lon, "day": stop.day,
        "arrival_time_formatted": _format_time(stop.arrival_time),
        "departure_time_formatted": _format_time(stop.departure_time),
        "duration_formatted": _format_duration(stop.duration),
    }


def _log_entry_to_dict(entry) -> dict:
    return {
        "time": round(entry.time, 2), "status": entry.status,
        "location": entry.location, "remarks": entry.remarks,
        "time_formatted": _format_time_of_day(entry.time),
    }


def _daily_log_to_dict(dl: DailyLog) -> dict:
    return {
        "day_number": dl.day_number, "date_label": dl.date_label,
        "log_entries": [_log_entry_to_dict(e) for e in dl.log_entries],
        "from_location": dl.from_location, "to_location": dl.to_location,
        "total_miles": dl.total_miles, "carrier": dl.carrier,
        "driver_name": dl.driver_name, "truck_number": dl.truck_number,
        "trailer_number": dl.trailer_number,
        "hours_off_duty": dl.hours_off_duty, "hours_sleeper": dl.hours_sleeper,
        "hours_driving": dl.hours_driving, "hours_on_duty": dl.hours_on_duty,
        "total_hours": round(dl.hours_off_duty + dl.hours_sleeper + dl.hours_driving + dl.hours_on_duty, 2),
    }


@api_view(["POST"])
def calculate_trip_view(request):
    try:
        data = request.data
        current_location = data.get("current_location", "").strip()
        pickup_location = data.get("pickup_location", "").strip()
        dropoff_location = data.get("dropoff_location", "").strip()
        cycle_used_hours = float(data.get("cycle_used_hours", 0))

        if not all([current_location, pickup_location, dropoff_location]):
            return Response({"error": "All location fields are required."}, status=400)
        if cycle_used_hours < 0 or cycle_used_hours > 70:
            return Response({"error": "Cycle hours must be 0-70."}, status=400)

        result: TripResult = calculate_trip(
            current_location=current_location, pickup_location=pickup_location,
            dropoff_location=dropoff_location, cycle_used_hours=cycle_used_hours,
            geocode_func=geocode, route_func=get_route
        )

        return Response({
            "success": True,
            "summary": result.summary,
            "stops": [_stop_to_dict(s) for s in result.stops],
            "daily_logs": [_daily_log_to_dict(dl) for dl in result.daily_logs],
            "route_waypoints": result.route_waypoints,
            "total_distance_miles": round(result.total_distance_miles, 1),
            "total_duration_hours": round(result.total_duration_hours, 2),
            "total_days": result.total_days,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response({"error": f"Calculation failed: {str(e)}"}, status=500)


@api_view(["GET"])
def health_check(request):
    return Response({"status": "ok", "service": "ELD Trip Planner API"})
