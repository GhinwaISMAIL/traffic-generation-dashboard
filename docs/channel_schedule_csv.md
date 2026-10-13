# Channel schedule CSV

Use **Testbed → Channel schedule → Upload CSV** to import calibrated channel
changes for the selected generated run. A local CSV path is also supported.
The existing manual table remains available. The uploader accepts files up to
1,024 MB; available memory limits how many expanded events can be processed.

## Required schema

```csv
at_s,target,direction,parameter,value
```

All five columns and all row values are required. Column order may vary, but
duplicate or additional columns are rejected. Use comma-separated UTF-8 text
(an optional UTF-8 BOM is accepted) and a decimal point for fractional numbers.

| Column | Type | Meaning |
| --- | --- | --- |
| `at_s` | Finite number | Seconds after traffic start, between zero and the selected run's duration, inclusive. Fractional seconds are accepted. |
| `target` | Text | A generated UE or experimental UE identifier for downlink; a cell identifier for uplink. See targets below. |
| `direction` | Text | `dl` or `ul`. |
| `parameter` | Text | `noise_power_dB` for noise, or `ploss` for path loss. The existing controls `riceanf`, `aoa`, `offset` and `forgetf` are also accepted. |
| `value` | Finite number | Your calibrated setting, in the parameter's native units. Noise and path-loss settings are in dB. No calibration, sign change or unit conversion is performed. |

Use one row per parameter change. To change both noise and path loss for one UE,
provide two rows. The same time can appear for different targets or parameters.
Duplicate combinations of time, resolved target, direction and parameter are
rejected, even when their values match.

Example values below illustrate the format only; replace them with your own
calibrated values:

```csv
at_s,target,direction,parameter,value
0,ue1,dl,noise_power_dB,-30
0,ue1,dl,ploss,10
0,ue2,dl,noise_power_dB,-35
60.5,ue1,dl,noise_power_dB,-25
60.5,cell1,ul,ploss,12
```

## Targets

- **Downlink:** `ue1`, `ue2`, and the other generated UE names in the run's MGEN
  manifest. For measurement runs, names such as `mobile_ue1` or `static_ue1` are
  resolved using that run's `ue_mapping.csv`. The mapping is displayed in the
  schema expander. Unknown or ambiguous names are rejected.
- **Uplink:** `cell1`, `cell2`, or another configured cell. The current backend's
  uplink model is shared by the UEs in a cell; per-UE uplink targets are rejected.
- **Groups:** `all_ues` and `cell1_ues` apply downlink changes to their UEs.
  `all_cells` applies uplink changes to every configured cell. Groups expand to
  individual targets before validation; overlapping group and individual rows
  cannot create duplicate changes.

## Import and save

1. Select the generated traffic run on the Testbed page.
2. Choose **Upload CSV** or **Local CSV file** and select the file.
3. Review validation, the change count, target counts and first/last times.
   The preview shows the first 20 normalized rows; all validated rows are kept.
4. Select the expected boot model and enable the schedule.
5. Press **Save channel schedule**. This replaces the run's saved schedule in
   `channel_schedule.json`, using the existing deployment schema. Uploading or
   previewing alone does not change the saved schedule or contact the testbed.

The uploader provides a header-only template and a download of the normalized
CSV. Large saved schedules are previewed instead of placed into the manual
editor; use a CSV to replace them. Invalid replacement files cannot save a
previous valid preview. While a TwinDash deployment holds the run lock, preview
remains available but saving or disabling the schedule is blocked.

## Timing and execution

The file describes channel parameter changes, separately from the application
session CSV. Changing channel settings does not regenerate the MGEN scripts.
The selected expected model is checked against the boot model; the CSV cannot
switch the channel family.

Each setting remains active until another event changes the same target,
direction and parameter. There is no interpolation, implicit reset or automatic
conversion from measured SINR/RSRP/CQI. Supply initial settings at `at_s=0` for
each control that needs a defined starting value. Otherwise its existing
testbed setting remains active until the first event.

Times follow the deployment's traffic clock. The current runner applies and
verifies events sequentially through SSH. File upload support does not add
high-frequency replay guarantees or make same-time events atomic. Verified
transitions are recorded in `logs/channel_state.json`.

The importer does not change execution duration or rescale timestamps. For a
cropped execution window, prepare offsets relative to that execution's start;
the measurement schedule's execution-window preview does not do this conversion
for the channel uploader.
