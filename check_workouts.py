"""
Checks Oura for new workouts and sends a phone notification for each one,
with your progress against the weekly goals in goals.json.

GitHub runs this every 15 minutes (see .github/workflows/check.yml).
It needs these secrets: OURA_CLIENT_ID, OURA_CLIENT_SECRET,
OURA_REFRESH_TOKEN, NTFY_TOPIC.
"""

import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://api.ouraring.com/oauth/token"
WORKOUT_URL = "https://api.ouraring.com/v2/usercollection/workout"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
GOALS_FILE = os.environ.get("GOALS_FILE", "goals.json")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
DAYS_TO_LOOK_BACK = 2  # catches workouts that sync late


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data)) as resp:
        return json.load(resp)


def get_access_token(state):
    """Reuse the saved access token, or swap the refresh token for a new one."""
    # A new OURA_REFRESH_TOKEN secret means you logged in again (e.g. with a
    # different account), so forget the old saved login and use the new one.
    secret_id = hashlib.sha256(os.environ["OURA_REFRESH_TOKEN"].encode()).hexdigest()
    if state.get("secret_id") != secret_id:
        for key in ("access_token", "expires_at", "refresh_token"):
            state.pop(key, None)
        state["secret_id"] = secret_id

    if state.get("access_token") and state.get("expires_at", 0) > time.time() + 60:
        return state["access_token"]

    # Oura may hand out a new refresh token each time, so we keep the latest one
    # in state. The GitHub secret is the fallback for the very first run.
    candidates = [state.get("refresh_token"), os.environ["OURA_REFRESH_TOKEN"]]
    for refresh_token in dict.fromkeys(t for t in candidates if t):
        try:
            tokens = post_form(TOKEN_URL, {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": os.environ["OURA_CLIENT_ID"],
                "client_secret": os.environ["OURA_CLIENT_SECRET"],
            })
        except urllib.error.HTTPError as err:
            print(f"Refresh token rejected ({err.code}), trying the next one")
            continue
        state["access_token"] = tokens["access_token"]
        state["expires_at"] = time.time() + tokens.get("expires_in", 3600)
        state["refresh_token"] = tokens.get("refresh_token", refresh_token)
        print(f"Refreshed Oura login. Permissions granted: {tokens.get('scope', 'not stated')}")
        save_state(state)  # save right away so a new refresh token is never lost
        return state["access_token"]

    raise SystemExit("No working refresh token. Run get_token.py again and update "
                     "the OURA_REFRESH_TOKEN secret.")


def load_settings():
    with open(GOALS_FILE) as f:
        settings = json.load(f)
    # Use your timezone so "today" and "this week" match your calendar
    os.environ["TZ"] = settings.get("timezone", "UTC")
    time.tzset()
    return settings


def week_start():
    today = dt.date.today()
    return today - dt.timedelta(days=today.weekday())  # Monday


def fetch_workouts(access_token, start=None):
    today = dt.date.today()
    start = start or min(week_start(), today - dt.timedelta(days=DAYS_TO_LOOK_BACK))
    end = today + dt.timedelta(days=1)
    query = urllib.parse.urlencode({"start_date": start.isoformat(),
                                    "end_date": end.isoformat()})
    req = urllib.request.Request(f"{WORKOUT_URL}?{query}",
                                 headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)["data"]
    print(f"Asked Oura for workouts from {start} to {end} ({dt.datetime.now():%H:%M} local): got {len(data)}")
    return data


def log_recent_workouts(access_token):
    """When nothing comes back, look further back so the log shows what Oura has."""
    recent = fetch_workouts(access_token, dt.date.today() - dt.timedelta(days=30))
    for w in recent:
        print(f"  {w['day']}  {w.get('activity')}  source={w.get('source')}")


def describe(workout):
    """Turn one Oura workout into a short message, e.g. 'Running · 42 min · 386 kcal'."""
    start = dt.datetime.fromisoformat(workout["start_datetime"])
    end = dt.datetime.fromisoformat(workout["end_datetime"])
    minutes = round((end - start).total_seconds() / 60)

    title = (workout.get("label") or workout.get("activity") or "Workout")
    title = title.replace("_", " ").title()

    parts = [f"{minutes} min"]
    if workout.get("calories"):
        parts.append(f"{round(workout['calories'])} kcal")
    if workout.get("distance"):
        parts.append(f"{workout['distance'] / 1000:.1f} km")
    if workout.get("intensity"):
        parts.append(workout["intensity"])
    return title, " · ".join(parts)


def matching_goal(workout, goals):
    """Which goal does this workout count toward? Looks at the activity and your label."""
    text = f"{workout.get('label') or ''} {workout.get('activity') or ''}"
    text = text.lower().replace("_", " ")
    for goal in goals:
        if any(word.lower() in text for word in goal["matches"]):
            return goal
    return None


def weekly_progress(workouts, goals):
    """Count this week's workouts per goal and build the progress lines."""
    monday = week_start().isoformat()
    counts = {goal["name"]: 0 for goal in goals}
    for workout in workouts:
        goal = matching_goal(workout, goals)
        if goal and workout["day"] >= monday:
            counts[goal["name"]] += 1

    done, lines = 0, []
    for goal in goals:
        target = goal.get("times_per_week", 1)
        count = counts[goal["name"]]
        if count >= target:
            done += 1
            lines.append(f"✅ {goal['name']}")
        else:
            extra = f" ({count}/{target})" if target > 1 else ""
            lines.append(f"⬜ {goal['name']}{extra}")

    if done == len(goals):
        summary = f"🎉 All {done} goals done this week!"
    else:
        summary = f"This week: {done} of {len(goals)} goals done"
    return summary + "\n" + "\n".join(lines)


def notify(title, message):
    payload = {"topic": os.environ["NTFY_TOPIC"], "title": title,
               "message": message, "tags": ["muscle"]}
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).close()
    print(f"Sent: {title} | {message}")


def main():
    settings = load_settings()
    goals = settings["goals"]
    state = load_state()
    access_token = get_access_token(state)
    workouts = fetch_workouts(access_token)
    if not workouts:
        log_recent_workouts(access_token)

    first_run = "seen" not in state
    seen = state.get("seen", {})  # workout id -> day, so we never ping twice

    if first_run:
        # Don't spam old workouts the first time; just say hello.
        notify("Oura pinger connected ✅", weekly_progress(workouts, goals))
    else:
        new = sorted((w for w in workouts if w["id"] not in seen),
                     key=lambda w: w["start_datetime"])
        for workout in new:
            title, stats = describe(workout)
            goal = matching_goal(workout, goals)
            if goal:
                title = f"{goal['name']} ✅  ({title})"
            else:
                stats += f"\n(Not linked to a goal. Oura called it \"{workout.get('activity')}\")"
            # Progress only counts workouts up to this one, so each ping is accurate
            so_far = [w for w in workouts if w["start_datetime"] <= workout["start_datetime"]]
            notify(title, stats + "\n\n" + weekly_progress(so_far, goals))

    seen.update({w["id"]: w["day"] for w in workouts})
    cutoff = (min(week_start(), dt.date.today() - dt.timedelta(days=DAYS_TO_LOOK_BACK))
              - dt.timedelta(days=1)).isoformat()
    state["seen"] = {i: day for i, day in seen.items() if day >= cutoff}  # keep it small
    save_state(state)
    print(f"Checked {len(workouts)} workouts.")


if __name__ == "__main__":
    main()
