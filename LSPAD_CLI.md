# lSPAD command guide

Reference for the TCP command protocol `lSPAD.exe` exposes on port 9999 (see
`sender_backend.py`'s `spad_sock` and `ssh_launcher.py`'s `send_lspad_cmd`).
Not derivable from this repo's code — this is the hardware/driver's own
command set, provided by the user from lSPAD's documentation.

All commands start with capital characters, followed by their parameters.
Parameters in `<>` are numerical values; parameters in `[]` are strings.
Commands can be separated by a line feed `\n`, so multiple commands can be
sent at the same time.

## Stop any currently running acquisition

`STOP`

Stops currently running acquisitions (if any).

## Classical intensity measurement

`I,<measurement time in ms>,<frames*>,<gate steps*>,<gate step size in ps*>`

(`*` = optional parameter.)

Returns the intensity data from the 320 pixels. If frame=1 and gate steps=1,
it is a list of 160 lines with on each line
`<pixel nr.>,<pixel counts>,<pixel nr.>,<pixel counts>`. Otherwise, on each
line is printed the iteration number followed by the 320 pixel count values.

## Scanning intensity measurement

`CI,<measurement time in us>,<frames>,<X-elements>,<Y-elements>,<external frame clock>,<gate steps*>,<gate step size in ps*>`

`CS,<measurement time in us>,<frames>,<X-elements>,<Y-elements>,<external frame clock>,<gate steps*>,<gate step size in ps*>`

If `<measurement time>` = 0, the external dwell clock is used.
`<external frame clock>`: 0 for no use of external clock, 1 to wait for
external frame clock. (`*` = optional parameter.)

`CI` returns the paths to the stored images (320 non-scaled images per
measured frame/gate step, plus the combined result). `CS` returns a stream
of the images — each dwell is a single byte in the stream, 320 non-scaled
images returned per scanned frame.

## Spectral measurement

`L,<measurement time in ms>,<frames>`

Returns the data directly: first line is wavelengths, followed by the 320
pixel counts on each consecutive line.

Additional spectral commands:
- `L,min,<wavelength in nm>` — set minimum wavelength
- `L,max,<wavelength in nm>` — set maximum wavelength

## Timestamping measurement

`T,<measurement time in ms>`

Returns the path to the stored data files.

Additional timestamping and TDC commands:
- `T,r,<enable>` — enable/disable raw data saving (0/1)
- `T,i,<enable>` — enable/disable image generation (0/1)
- `T,h,<enable>` — enable/disable histogram functionality (0/1)
- `T,hx,<bin width in ps>` — set histogram bin width
- `T,hs,<enable>` — split histogram by dwells (0/1)
- `T,a,<measurement time in ms>` — perform a new pixel alignment measurement; returns when complete
- `T,c,1` — perform a new calibration; returns when complete
- `T,v,1` — check if the TDC is calibrated; returns calibration state

## Timestamping measurement in real-time streaming mode

`S,<measurement time in ms>` — string-based stream. Format per line:
`<master=1/slave=0 boolean>,<pixel nr. / marker nr.>,<coarse counter value>,<TDC value>`

`SB,<measurement time in ms>` — binary-based stream. Format per record:
1 byte master=1/slave=0 | 1 byte pixel nr./marker nr. | 2 bytes (16-bit int)
coarse counter value | 3 bytes (24-bit int) TDC value.

(See the pixel look-up table for converting the master/slave pixel number to
the actual pixel number for S/SB modes — this is `PIXMAP`/`master_loc`/
`slave_loc` in `sender_backend.py`.)

`SH,<measurement time in ms>,<histogram bin width in ps>,<split dwells>` —
`<split dwells>`: 0 for no splitting, 1 to split the histogram at each
dwell. Returns histograms for both master/slave parts: master/slave
time-axis on the first line, followed by each pixel histogram on a new
line; second part repeats for the remaining pixels.

## Pulse width or distance measurement

`P,<measurement time in ms>,<pixel>,<pulse mode>`

`<pixel>`: SPAD pixel number 0–319. `<pulse mode>`: 0 for pulse width, 1 for
pulse distance. Returns the path to the stored data file.

## Calibrate the system

`CALIB,<mode>`

`<mode>`: -1 to enable/disable all corrections, 0 for noise calibration, 3
for breakdown voltage calibration, 4 for gate ratio calibration (improves
noise correction accuracy when gating is enabled). Returns when the
measurement has completed.

## Get current operating temperatures and running frequencies

`R`

Returns FPGA/PCB temperatures (°C) and measured laser/frame/line/dwell
clock frequencies. Format:
`<FPGA master temp>,<FPGA slave temp>,<main PCB temp>,<chip PCB temp>,<laser freq>,<frame freq>,<line freq>,<dwell freq>`.
For systems with a humidity sensor:
`<FPGA master temp>,<FPGA slave temp>,<main PCB temp>,<main PCB temp 2>,<chip PCB temp>,<relative humidity %>,<laser freq>,<frame freq>,<line freq>,<dwell freq>`
(this is the 10-field format `ssh_launcher.py`'s `query_r`/`get_dwell_freq`
and `sender_backend.py`'s `run()` parse).

## Get system information

`D`

Returns FPGA/software/firmware/hardware version info and enabled features.

`D,[dir]` — set the path for the software to save data/images; returns the
folder path if it exists.

## Set the measurement time-out

`TO,<time in seconds>`

Sets the new time-out (value rounded to 5-second increments).

## Set the fan speed

`F,<speed>`

`<speed>`: 0 disabled, 1 enabled at full speed. Returns confirmation.

## Enable/disable dummy clock markers

`N,<enable>`

`<enable>`: 1 to activate dummy markers, 0 to disable. Returns confirmation.

## Enable/disable system power

`POW,<enable>`

`<enable>`: 1 to activate VDD, 0 to disable. Returns confirmation.

## Set/get voltages

`V,<Vex>` — set excess voltage, range 4–9 V. Returns confirmation.

`V` — get current voltages. Returns `<Vex>`.

## Program the gate

`G,<gate enable>,<gate width>,<gate offset>`

`<gate enable>`: 1 to enable gating, 0 to disable. Returns confirmation.

## Program the pixel mask from a file

`M,[full path to file]`

Returns confirmation when mask programming is complete. (Used by
`ssh_launcher.py`'s `launch_node()`.)

## Close the software

`QUIT`

Closes the software and brings the detector into a safe operating regime.
