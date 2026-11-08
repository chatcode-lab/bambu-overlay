# Building bambu-overlay with Claude Code

`bambu-overlay` was built with Claude Code in one terminal session hosted by
[chatcode.dev](https://chatcode.dev), during the night and evening of 8 November 2026.
This file connects the human direction in that session to the commits that followed.
It is a record of the feedback loop, not a claim that the project was made without
human judgment.

The prompts are quoted as typed, lightly edited: typos and slips of the keyboard are
fixed and abbreviations spelled out. Attached
screenshots are described rather than republished. Secrets, local paths, raw tool
output and session-management replies (such as "yes" to "push now?") are left out.
Several prompts were sent while the agent was still working on the previous one,
which is why some of them change direction mid-task.

## 1. A read-only overlay for the X2D

> Let's build a local hosted HTML overlay for OBS stream of 3D printing jobs from
> Bambu Lab X2D printer:
>
> 1. It should poll current job data over MQTT, I put access code and IP to .env
>    (also check if IP can change)
> 2. *[Screenshot of the stream on Twitch without any overlay]* Here's how current
>    stream looks like on Twitch without any overlay
> 3. I crop source 1920x1080 stream to 1280x720 to blow up and for faster encoding /
>    less bandwidth
> 4. Make several versions of fancy layouts for overlay (switchable). They should be
>    semitransparent and blend nicely
> 5. There should be such info as % progress (with progress bar), time left, nozzle
>    temperature, plate temperature, model preview, filament info, etc – everything
>    you decide useful
>
> As possible solutions and references you can use this:
> https://synman.github.io/bambu-printer-manager/bambu-mcp/tools/state/#get_job_info
> https://github.com/drkostas/bambu.local
> or whatever you find better solution
>
> Use all connections read-only, no actions should be ever performed on the printer
> for safety

Two corrections arrived while the agent was probing the printer:

> There's no external storage connected to X2D, only internal

> yes, IP starts from 192

The first one ruled out FTPS (on the X2D it only exposes external storage), so the
job file is read over the printer's own file channel instead. The second one fixed a
typo in `.env`.

## 2. macOS refuses the local network

The printer answered `ping`, but Python got `[Errno 65] No route to host`. The user
pasted a hint from the bambu.local README:

> From bambu.local github: On macOS, macOS can refuse a program access to the local
> network with [Errno 65] No route to host, while other programs on the same Mac
> reach the printer. If ping answers and Python does not, look at Privacy & Security,
> Local Network in System Settings.

> No python in the list, let's try again

A process started from a background terminal has no app for macOS to ask, so it never
appears in that list. The agent wrapped the server in a small menu-bar app
(`tools/BambuOverlay.swift`) that macOS can prompt for.

> I clicked Allow

## 3. Camera crop, LAN access and defaults

> I've set up demo overlay mode, it works
>
> Also: let's adjust crop position on video stream via OBS ws server. In printing
> mode it should be top: 80, bottom: 280. In idle it should be top: 180, bottom: 180
> (adjustable). Crop position should change smoothly
>
> Make web server available on local network like miniquest.local:8787
>
> And hide maker credit / model link / model name by default. Keep file name shown by
> default

> ETA doesn't make sense on live stream since it's time zone dependent. Could be time
> elapsed instead

> *[Screenshot of the overlay on the stream]* Also some labels are hardly readable
> (bottom right)
>
> I enabled the WS server

## 4. A bigger model preview in the lower third

> *[Screenshot of the lower-third layout]* I like this layout, but I would utilize
> this lower left corner on the video more to display bigger model preview since this
> corner space doesn't make sense in the video anyway, so, it could be larger model
> preview that is above the bottom line with all other controls

## 5. A Winamp-style audio meter

> Add simple two-channel volume beat animation (two segmented bars) with some old
> Winamp vibe to the bottom right end in this overlay (could be in others too) using
> WS server's InputVolumeMeters

> MugenRadio is backup, it's muted now, try to check it automatically

> I would make the meter semitransparent more blended into overlay background, maybe
> without intense borders / container

> *[Screenshot of the meter in the lower third]* there's weird left border now. also
> I would decrease height so to level other controls

> What if make horizontal direction?

> Much better now! Create a free private repo in chatcode-lab/bambu-overlay or
> something like this and push there without secrets

Result: [`07c0932`](https://github.com/chatcode-lab/bambu-overlay/commit/07c0932)

## 6. Keep it light in the background

> Also make sure that CPU usage is relatively low for all our machinery, it's
> supposed to work in background

Result: [`8411fc4`](https://github.com/chatcode-lab/bambu-overlay/commit/8411fc4)

## 7. The wrong plate on stream

> *[Screenshot of the stream: the lower third shows "Plate 3/4" with a long bar as
> the preview, while two seats are being printed]* Bug: it shows plate 1/4 model in
> preview but it's really printing plate 3/4 model

The printer starts a job a few seconds before it saves the job's `.3mf`, and the
overlay had fetched an older file with the same name from a morning print of plate 1.

Result: [`67bf455`](https://github.com/chatcode-lab/bambu-overlay/commit/67bf455)

## 8. Finished, but still "printing"

> Move current plate info to the left under the model preview
>
> Also, there's intermediate state when printing is finished and the chamber air is
> being purified (still showing "printing" state) when it's already reasonable to
> move crop rectangle down, it's essentially finished at the moment

The agent recorded the printer's raw reports through the end of the print before
changing anything:

> It's done now

Result: [`7e3046f`](https://github.com/chatcode-lab/bambu-overlay/commit/7e3046f)

## 9. Going public

> Let's make the repo public and describe the case in chatcode.dev/lab/
