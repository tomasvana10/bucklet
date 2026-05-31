# `@emcmarket/scraper`

This package implements a JSMacros script to scrape shop data (manually or automatically) from a server.

## Prerequisites

- An access key for `@emcmarket/emcapi-proxy`
- A push key for `@emcmarket/db`

## Setup

1. Install the latest version of JSMacros from [EastArctica's fork](https://github.com/JsMacrosCE/JsMacros/releases/).

2. Add a JSMacros service named `__SHOP_SCRAPER__` and point it to `dist/index.js`

3. Enable and start the service.

4. Set your keys:
```
/@scraper keys emcApiProxyKey set <key>
/@scraper keys marketDbAccessKey set <key>
(optional) /@scraper keys signozKey set <key>
``` 

## Scraping Manually

1. Enable automatic resolution of town/nation names from chat commands: `/@scraper config autoTownyEntityResolution set true`

2. Visit your desired nation/town (e.g. `/n spawn Japan`, which detects the nation name as `Japan` and the town as `Tokyo`)

3. Scrape and upload: `/@scraper scrape`

## Unique Transactions

To run multiple scrapes at the same place (likely because it cannot all be loaded at once), you can utilise unique transactions to prevent submitting duplicate entries. Technically the database/data analysers won't care, but it's a waste.

Begin a transaction using `/@scraper tx begin`, scrape as many times as you'd like using `/@scraper scrape`, and commit your data with `/@scraper tx commit`.

## Scraping Automatically

The scraper allows you to manage a centralised catalogue of spawns to visit and scrape. It provides various tools to improve the reliability of this process.

> [!IMPORTANT]
> These commands have only been tested to work on Void Linux (my distro of choice for automated scraping). However, if you wish to use a different distro, you will likely only have to change the `.desktop` file and use your own service manager instead of `runit`, which this guide uses.

### Performance tips

Some general tips you may find helpful to improve performance if you have a VM with limited resources.

- Use the `Digs' Simple Pack` resource pack.
- Allocate the same amount of RAM for the minimum (Xms) and maximum (Xmx) values. A solid amount of RAM for 1.21.11 is 3GB.
- Use the `EntityCulling`, `FerriteCore`, `ImmediatelyFast`, `Lithium`, `ModernFix`, and `Sodium` mods.
- Set `Particles` to `Minimal`.
- Decrease FPS to `20`. If you are running macros (see below), you may want to increase this value to increase their reliability.
- Decrease all quality-related settings to their worst.

### General tips

- Turn off your screensaver and ensure your power management settings aren't configured to turn off your machine after a certain amount of time.

### Setup

Synchronise required files into `$HOME/emcmarket` using `scripts/sync.sh`. This file updates the directory with up-to-date scripts from the repository before launching the game.

### Auto feeding

Automatically feed the player based on these settings under `/@scraper auto config`:

```
autoFeed: true|false
autoFeedWasteFood: true|false
playerStarvingTactic: dontRunMacros|conclude|nothing,
```

### Macros

Macros can be used to improve bot legitimacy as well as scrape spawns that exceed the player's (or server's) render distance. 

Catalogue items with attached macros will automatically use [Unique Transactions](#unique-transactions) (if the `postMacroAction` is `scrape`, and not `nothing`).

Commands to manage macros are as follows:

```
auto catalogue (nation|town) macro <name> (deleteAll|getHead|playAll|pop|record)
auto stopMacro
```

Macros are structured in the LIFO format, meaning you can only ever add or remove macros from the end of the stack.

When recording a macro, it is important to either:

1. Be at the spawn of your nation/town if it is the first macro you are recording, or,

2. Have ran `playAll` to reach the end of the existing macro stack.

### Managing the automation catalogue

The automation catalogue is a set of spawn locations to visit and scrape.

Commands to manage your automation catalogue are as follows:

```
auto catalogue (nation|town) add <name> <weight> <postMacroAction>
auto catalogue (nation|town) remove <name>
auto catalogue (nation|town) config <name> (<postMacroAction>|<weight>) (get|set <value>)
```

The automation scheduler uses the **Deficit Weighted Round Robin (DDWR)** algorithm to control what spawns are visited. Here is a simple, practical example to explain DDWR:

1. Nation `USA` is added to the catalogue with a weight of 100.

2. Nation `Canada` is added to the catalogue with a weight of 200. 

3. Every day, for 10 days, the user runs their automation.

4. At the end of the 10 days, `USA` will have been visited between `3-4` times and `Canada` will have been visited between `6-7` times.

DDWR is great for automated scraping as it lets you fine tune how often you want to visit and scrape a certain spawn.

> [!IMPORTANT] 
> Adding catalogue entries with a weight of 100 is highly recommended for catalogue maintainability as it allows you to easily configure relative weightings, since floating point values are not accepted by the command.

> [!NOTE] 
> By nature of the scraper's DDWR implementation, new catalogue entries with no visits would be constantly visited until they were aligned with the rest of the catalogue and weights. The script solves this by allocating the entry with the expected amount of visits so automation continues smoothly.


### Minecraft client and programmatical launching

A normal Minecraft client is required for automated scraping. You can not use any headless client. 

> [!NOTE] 
> This guide assumes you are using [Prism Launcher](https://prismlauncher.org/), due to it's simplicity and provision of a CLI tool to launch the an instance.

To launch your desired client, you can utilise `scripts/launch-client.sh`:

```sh
./scripts/launch-client.sh "My_Instance" "play.earthmc.net" "FuriousDestroyer"

# or, if you want to enforce that a proxied connection must be available:

./scripts/launch-client.sh "My_Instance" "play.earthmc.net" "FuriousDestroyer" "true"
```

To automatically launch your client on system start, you can create a desktop entry at `~/.config/autostart/prismlauncher.desktop`:

```
[Desktop Entry]
Type=Application
Name=EMCMarket Scraper Client
Exec=/home/tomas/app/packages/scraper/scripts/launch-client.sh "1.21.11" "play.earthmc.net" "BrotherDay"
Hidden=false
X-XFCE-Autostart-Override=true

```

### Automation agents

The scraper comes with a premade agent - `workflows/agent1.js`. This agent runs whenever you log in to `*.earthmc.net`, and commences automation. You can modify it however you wish to suit your needs or create your own. The `_common.js` module provides necessary functions and data which you can import.

To configure the agent to run automatically, add a JSMacros event script listening to `JoinServer`. Set the script to your desired agent and enable the listener.

### Automation conclusion

What to do on automation conclusion controlled by `automationConclusionAction`. Values are `nothing`, `exitGame`, and `shutdownHost`.

Automated host shutdown works through an intermediary file which the script writes to. `inotifywait` is used to detect any changes and then shutdown the host.

To configure automated host shutdown, follow these steps:

1. Set `linuxOsUser` to your linux user's name.

2. Install `inotify-tools`

3. Create a runit service directory and run script:

```sh
sudo mkdir -p /etc/sv/scraper-shutdown
```

Save the following to `/etc/sv/scraper-shutdown/run`:

```sh
#!/bin/sh
exec /home/USER/emcmarket/scripts/shutdown-listener.sh USER "sudo poweroff"
```

Make sure to update the path to the script, your user's name, and the shutdown command.

4. Make the run script executable and enable the service:

```sh
sudo chmod +x /etc/sv/scraper-shutdown/run
sudo ln -s /etc/sv/scraper-shutdown /var/service/
```

### Proxying your connection (1.21.11+)

Since SocksProxyClient isn't available anymore, here is how to set up a SOCKS5 proxy for Minecraft.

1. Install `redsocks`, `iptables` and `bind-utils`.

2. Configure `redsocks` in `/etc/redsocks.conf`:

```conf
base {
    log_debug = off;
    daemon = off;
    redirector = iptables;
}
redsocks {
    local_ip = 127.0.0.1;
    local_port = 12345;
    ip = PROXY_IP;
    port = PROXY_PORT;
    type = socks5;
    login = "USERNAME";
    password = "PASSWORD";
}
```

3. Create service directories:

```sh
sudo mkdir -p /etc/sv/mc-proxy/log
sudo mkdir -p /var/log/mc-proxy
```

4. Create the run script in `/etc/sv/mc-proxy/run` (you can add any IP or domain name to `SERVERS` to enable a proxied connection for it): 

```sh
#!/bin/sh

exec 2>&1

SERVERS="
play.earthmc.net
"

iptables -t nat -D OUTPUT -p tcp -j MC_PROXY 2>/dev/null
iptables -t nat -F MC_PROXY 2>/dev/null
iptables -t nat -X MC_PROXY 2>/dev/null

iptables -t nat -N MC_PROXY

for entry in $SERVERS; do
    case "$entry" in
        [0-9]*.*)  ip="$entry" ;;
        *)         ip=$(dig +short "$entry" \
                     | grep -E '^[0-9]' | head -1) ;;
    esac
    [ -n "$ip" ] && iptables -t nat -A MC_PROXY \
        -d "$ip" -p tcp -j REDIRECT --to-ports 12345
done

iptables -t nat -A OUTPUT -p tcp -j MC_PROXY

exec redsocks -c /etc/redsocks.conf
```

5. Create the finish script in `/etc/sv/mc-proxy/finish` to clear `iptables` rules:

```sh
#!/bin/sh

iptables -t nat -D OUTPUT -p tcp -j MC_PROXY 2>/dev/null
iptables -t nat -F MC_PROXY 2>/dev/null
iptables -t nat -X MC_PROXY 2>/dev/null
```

6. Create the log script in `/etc/sv/mc-proxy/log/run`:

```sh
#!/bin/sh

exec svlogd -tt /var/log/mc-proxy
```

7. Make the scripts executable:

```sh
sudo chmod +x /etc/sv/mc-proxy/{run,finish}
sudo chmod +x /etc/sv/mc-proxy/log/run
```

8. Enable and start `mc-proxy`:

```sh
sudo ln -s /etc/sv/mc-proxy /var/service/
sudo sv status mc-proxy
```

9. Check the rules are active and `redsocks` is running:

```sh
sudo iptables -t nat -L MC_PROXY -n
tail -3 /var/log/mc-proxy/current
```