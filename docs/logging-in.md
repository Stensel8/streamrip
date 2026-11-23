[← Back to the README](../README.md)

# Logging in

streamrip asks for what it needs the first time you use a source and saves it in
the config. If a login stops working, it asks again.

The isolated browser window mentioned below is only offered when it can work.
The `browser` extra must be installed (the installers include it), and on Linux
there has to be a screen to open it on. In a container or over SSH, your browser
runs somewhere else and can't reach streamrip. Qobuz and Deezer then ask you to
log in by hand, and for Spotify you paste the address.

## Qobuz

You need a subscription, or albums you bought. Qobuz has no device login like
Tidal, but once you're logged in on the website it stores a login token, and
streamrip offers three ways to get it:

1. **An isolated browser window.** You log in, and streamrip grabs the token by
   itself. It uses a Chrome-family browser you already have (Chrome, Edge,
   Brave, Chromium, ...) with a throwaway profile, so there's no existing
   session in the way and nothing gets downloaded. If you only have Firefox or
   LibreWolf, it asks before downloading Playwright's own small browser.
2. **Your own browser**, opened at [the login page](https://play.qobuz.com/login).
   Log in first, *then* paste the short script streamrip prints into the console
   (F12). Qobuz reloads the page when you log in, which would stop a script you
   pasted earlier. The script reads the token Qobuz's web player already keeps
   in `localStorage`. It's just a few lines of JavaScript in the browser you
   already have open.
3. **By hand.** Copy `user.id` and `user_auth_token` from DevTools → Network
   yourself.

If nothing comes in within a few minutes, options 1 and 2 fall back to entering
it by hand. Either way, the login is saved.

## Tidal

You need a subscription. streamrip shows a link you can open on any device to
log in. You do this twice, because Tidal serves hi-res (up to 24-bit, 192 kHz)
and CD quality through two separate logins. Each track is requested in hi-res
first, and falls back to 16-bit FLAC when Tidal has no hi-res master. Set
`hires_client = false` in `[tidal]` to skip the hi-res login and only get 16-bit
FLAC.

## Deezer

Deezer has no sign-in for other apps, and its email and password login is
behind a captcha. streamrip logs in with the `arl` cookie of a logged-in web
session instead, and offers two ways to get it:

1. **An isolated browser window**, the same one as for Qobuz. You log in there
   yourself. streamrip only keeps the `arl` cookie and never sees your password.
2. **By hand.** Copy the `arl` cookie from your browser's DevTools
   ([how to find it](https://github.com/nathom/streamrip/wiki/Finding-Your-Deezer-ARL-Cookie)).

The quality depends on your subscription. Lyrics come from LRCLIB, as with
Qobuz (see [Lyrics](lyrics.md)).

## SoundCloud and Spotify

SoundCloud needs no login. Spotify needs an app of your own and a one-time login
in your browser; see [Spotify](spotify.md).
