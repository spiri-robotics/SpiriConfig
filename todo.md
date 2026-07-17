

Global settings used by all apps? Only one I can really think of is hostname/robot-name. The design of SpiriSynq mostly avoids the need for our actual apps/actions as they auto-discover. We need to be able to pass hostname in to robots, and be able to change it. This is one of those finicky systems where we need an OS level abstraction, I'm fully planning on shipping real drones without systemd using yocto, so we need to support different ways of changing hostname. (Note, we could bypass the need for this with zenoh router I think, it will handle re-rooting paths to a known key. Maybe something I should work on first. Make zenoh router required by default?).

Haven't tested mobile respnsiveness. Probably not a problem.


Way to clean out old docker data, see how much space is being used/waster, general disk management.

We should be able to get more fine-tuned status then just off, partial, up, for multi-node services. Also report health check services, maybe click to see each services status.


Pre-load app stores (git repos) through env vars. Probably some in-app fixture to pre-load our public app store.


First-run is a bit of a conondrum. How do we specify the *initial* state of a robot? Do we do it as part of this, or as a seperate script? I'm tempted to make it a seperate startup script that we apply to our own images, but being able to apply or upload a state file that makes our robot match a given state would be very nice. Would need another abstraction, some layer where we can discover current state and push remote state. Not the worst idea... but a big change. Mostly this means we have an idempotent layer of... what? Diffing the file, and running commands? Mapping commands to state? And making sure it runs in the right order? A difficult bit of machinery.


---

Further down the road, 

Companion computers can have companion computers, an nvidia orin strapped to an RPI. We need to support either docker swarm or multiple docker daemon connections for compute heavy workloads. Docker swarm I think would require us to rewrite every service? That won't work, but swarm is defenitely better for clusters... This also requires a whole lot of other infra to do properly, like NFS stores probably. Not sure how to square this. Docker swarm first leads to poor developer UX. Run both a swarm node and a native node means massive data duplications. Docker swarm would only really be better for large-scale compute clusters. Leaning towards supporting multiple docker daemons.

## probably as raw plugins not docker containers

Host network configuration, using netplan test and netplan apply. A bit tricky because the users session *will* die, so we need to bypass the normal nicegui paths and redirect them to like <new_ip>/confirm_network or something. Or just check when a user has hit the endpoint after a test? Tricky flow.

Modem management for cell modems.

Network management for rajant.

Network management for wfb-ng.

The tailscale plugin is a good idea, better licensing than zerotier. Still need to set up our infra to manage it though.

Disk usage plugin, see where your storage space is being used (duc based?).