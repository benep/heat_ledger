# Installation

Heat Ledger is installed with **HACS**, the community store for Home Assistant. If you do not have HACS yet, install
it first: [hacs.xyz](https://hacs.xyz/docs/use/).

```mermaid
flowchart LR
    A["1. Add the repository<br>to HACS"] --> B["2. Download<br>Heat Ledger"] --> C["3. Restart<br>Home Assistant"] --> D["4. Add the<br>integration"]
```

## 1. Add the repository to HACS

Heat Ledger is not in the HACS list yet, so you tell HACS where to find it once:

<div class="steps" markdown>

1. Open **HACS** in the sidebar.
2. Click the three dots :material-dots-vertical: at the top right and choose **Custom repositories**.
3. Enter `https://github.com/benep/heat_ledger` as **Repository** and choose **Integration** as **Type**.
4. Click **Add** and close the dialog.

</div>

## 2. Download Heat Ledger

<div class="steps" markdown>

1. Search for **Heat Ledger** in HACS and open it.
2. Click **Download** at the bottom right and confirm.

</div>

## 3. Restart Home Assistant

Home Assistant only finds a new integration after a restart: **Settings → System**, the power button
:material-power: at the top right, **Restart Home Assistant**.

## 4. Add the integration

Click this button, or go to **Settings → Devices & services → Add integration** and search for **Heat Ledger**:

[![Add Heat Ledger to Home Assistant](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=heat_ledger)

The setup starts right away. Continue with [Setup](setup.md).

??? question "Updates"

    HACS shows a new version of Heat Ledger like any other update under **Settings → Updates**. Your counters keep
    their values across updates and restarts.
