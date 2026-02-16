# Klipper Camera Watchdog

This small python script checks if the crowsnest camera is available at `localhost:8080/snapshot`.
If it is not available, it restarts the `crowsnest` service and check again after a delay.

Each attempt to restart the camera daemon is done with increasing delay, to account for slow-initializing hardware and software. 
If the restarts fail after a set number of tries (default 5), the script enters a degraded state, where onlt one in 10 invocations will trigger the regular restart procedure.

The script is installed as a systemd timer configured to run once every 5 minutes.

# Installation

An ansible installation script is provided. It handles copying the script over to the printer host, creating and enabling the required systemd unit and timer files.

```bash
# Edit the inventory (adjust for your printer hosts)
vim inventory.yaml

# run the script
ansible-playbook -i inventory.yaml deploy.yaml
```
