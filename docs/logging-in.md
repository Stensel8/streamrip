[← Back to the README](../README.md)

# Logging in

streamrip asks for what it needs the first time you use a source, and saves it
in the config. A login that stops working is asked for again.

The isolated browser window below is only offered where it can work: with the
`browser` extra installed (the installers include it) and, on Linux, a screen to
open it on. In a container or over SSH the other ways are offered.

- **Qobuz** needs a subscription, or downloads the albums you bought. Qobuz's
  web login has no device-code flow to poll (unlike Tidal's), but it does save
  a working login token for itself the moment you're logged in, so streamrip
  offers a choice of how to grab that:
  1. **An isolated browser window** that logs in and captures the token with
     no further input from you. Drives a Chrome-family browser already on
     your machine (Chrome, Edge, Brave, Chromium, ...) in a throwaway
     profile — no existing session to work around, nothing downloaded. If
     none is found (Firefox/LibreWolf-only setups), it asks before
     downloading Playwright's own small browser instead.
  2. **Your own browser**, open to [the login page](https://play.qobuz.com/login).
     Log in first, *then* paste the short script streamrip prints into the
     console (F12) — Qobuz reloads the page on login, which would stop the
     script if pasted beforehand. It reads the token Qobuz's own web player
     already saves for itself in `localStorage`. Nothing is downloaded or
     driven; a few lines of JavaScript run in the browser you already have
     open.
  3. **By hand**: copy `user.id` and `user_auth_token` from DevTools →
     Network yourself.

  Options 1 and 2 fall back to manual entry if nothing arrives within a few
  minutes. Credentials are saved for later use either way.
- **Tidal** needs a subscription. streamrip shows a link to log in with on any
  device, twice: Tidal serves hi-res (up to 24-bit, 192 kHz) and CD quality
  through two separate logins. Each track is asked for in hi-res first and falls
  back to 16-bit FLAC when Tidal has no hi-res master. `hires_client = false` in
  `[tidal]` skips the hi-res login and gets 16-bit FLAC only.
- **Deezer** has no sign-in for outside apps, and its email and password login
  is behind a captcha. What streamrip logs in with is the `arl` cookie of a
  logged-in web session, so it offers a choice of how to get it:
  1. **An isolated browser window**, the same one Qobuz uses (see above). You
     log in there yourself; streamrip keeps only the `arl` cookie once you're
     in, and never sees your password.
  2. **By hand**: copy the `arl` cookie from your browser's DevTools
     ([how to find it](https://github.com/nathom/streamrip/wiki/Finding-Your-Deezer-ARL-Cookie)).

  The quality follows your subscription. **Deezer does not have lyrics for every
  track**, so streamrip looks the others up on LRCLIB (see [Lyrics](lyrics.md)) and
  says so when you use Deezer; `lyrics = false` in `[downloads]` turns that off.
- **SoundCloud** needs nothing.
- **Spotify** needs an app of your own and a login in your browser, once. See
  [Spotify](spotify.md).
