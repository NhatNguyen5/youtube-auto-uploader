"""Messages safe to show in the CLI and the desktop window."""


def format_status_line(story_id, role, job):
    """One status line for the CLI and the desktop window."""
    url = (
        f"https://www.youtube.com/watch?v={job['video_id']}"
        if job.get("video_id")
        else ""
    )
    detail = job.get("error", "")
    if job.get("log_error"):
        detail = f"{detail} upload-log: {job['log_error']}".strip()
    return f"{story_id}/{role}: {job['stage']} {url} {detail}"


def public_error(error):
    """Text for an exception. Only ValueError text is shown as-is.

    Other exceptions can carry credential-bearing request URLs, so those stay
    generic. ValueError messages raised by this project must not include a
    token, secret, or full request URL.
    """
    if isinstance(error, ValueError):
        return str(error)
    message = (
        f"Error ({type(error).__name__}). "
        "Check configuration, credentials, and saved status; no history was reset."
    )
    if isinstance(error, FileNotFoundError):
        message += (
            " Copy examples/config.example.json to config.json and set your channel ID."
        )
    return message
