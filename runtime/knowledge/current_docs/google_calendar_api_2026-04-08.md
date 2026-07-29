# Google Calendar API Notes

Captured: 2026-04-08

Why this matters to Mike:
- Mike uses Google Calendar MCP tools to inspect calendars and create, update, or delete events.
- These notes align that behavior with the official Calendar API reference.

Key points:
- `calendarList.list` is the official method to enumerate calendars available to the connected account.
- `events.insert` creates a new event with `POST /calendar/v3/calendars/{calendarId}/events`.
- The special calendar identifier `primary` can be used to target the signed-in user's primary calendar.
- Creating an event requires valid `start` and `end` values; timed events use RFC3339 date-time values, while all-day events use `date`.
- Official scopes for event creation include `https://www.googleapis.com/auth/calendar` and narrower event-oriented calendar scopes.
- The `sendUpdates` option controls attendee notifications, and Google warns that using `none` can have sync side effects in some scenarios.

Operational notes for Mike:
- Default to the `primary` calendar unless the user names another calendar.
- For normal one-off events, always include a clear title and coherent start/end values.
- Be careful with attendee notifications and time zones.

Sources:
- https://developers.google.com/workspace/calendar/api/v3/reference/calendarList/list
- https://developers.google.com/workspace/calendar/api/v3/reference/events/insert
