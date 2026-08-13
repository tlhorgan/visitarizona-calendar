# Visit Arizona Events Calendar

Creates a subscription-ready iCalendar feed from the public events listed at
[Visit Arizona](https://www.visitarizona.com/events).

The included GitHub Actions workflow refreshes the calendar every day and can
also be run manually from the **Actions** tab.

## Set up on GitHub

1. Create a new public GitHub repository named `visitarizona-calendar`.
2. Upload all files and folders from this project, including `.github`.
3. Open **Actions**, select **Update Visit Arizona calendar**, and click
   **Run workflow**.
4. After the workflow succeeds, subscribe to:

   `https://raw.githubusercontent.com/YOUR-USERNAME/visitarizona-calendar/main/visitarizona.ics`

Replace `YOUR-USERNAME` with your GitHub username. If your username is
`tlhorgan`, the address will be:

`https://raw.githubusercontent.com/tlhorgan/visitarizona-calendar/main/visitarizona.ics`

## Run locally

```bash
python -m pip install -r requirements.txt
python generate_calendar.py
```

The script discovers event pages through the site's XML sitemap, parses event
details, removes duplicates, ignores expired events, and writes
`visitarizona.ics`. If the source site changes and zero events can be parsed,
the script exits with an error rather than overwriting a working calendar with
an empty file.
