[← Back to the README](../README.md)

# Logging in

streamrip asks for what it needs the first time you use a source and saves it in
`state.toml`, next to `config.toml`. If a login stops working, it asks again.

```bash
streamrip accounts            # which services you are logged in to
streamrip verify              # asks every service: does it work, what is the plan
streamrip verify --fix        # and logs in to what needs it
streamrip login qobuz         # log in, or log in again (without a name: it asks)
streamrip logout qobuz        # forget a login; the app you made for it stays
```

`verify` shows the plan of each account (Qobuz's subscription, Deezer's licence,
Tidal's best quality, Spotify's Premium), says when your `quality` setting asks
for more than the plan gives, and exits 1 when a saved login no longer works.

The names are `qobuz`, `tidal`, `deezer` and `spotify`; `tidal-library` is Tidal's
official API, for [playlists](playlists.md). `streamrip web` shows the same
accounts and shares them.

The isolated browser window mentioned below is only offered when it can work.
On Linux there has to be a screen to open it on. In a container or over SSH, your browser
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

SoundCloud works as a guest, and with a login it gives better quality and has
higher limits. `streamrip login soundcloud` asks for the `oauth_token` cookie of
your login: log in at soundcloud.com, open DevTools, Application (or Storage),
Cookies, and copy its value. Leave it empty to stay a guest. When SoundCloud does
not take a saved login, streamrip says so and goes on as a guest.

Spotify's default needs no app of your own: `streamrip login spotify` shows a
pair link, and you open it on any device and approve it with the code. Your own
app is the other way (`streamrip login spotify --configure`), and the account
that owns it needs Premium. See [Spotify](spotify.md).
