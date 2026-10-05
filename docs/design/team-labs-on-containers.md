# Team labs on container nodes

Status: design only. No code in this PR. Stacked on #45 (merge #45 first).

## Problem

A team lab (several agents, each with its own nodes, stages and brief) is refused for container labs
(`create_topo_run`: "a container lab can't take a team yet"). The refusal is the only blocker in the form; the
machinery below is already shared by VM and container labs. Each piece needs a check against container nodes.

## What a team already does (VM labs)

| Piece | Where | Works for containers? |
|---|---|---|
| Members, stages, nodes per member | `validate_team`, `topo_team_phase` | Yes, nothing VM-specific |
| One ssh key per member, installed on each node it may touch | `member_keys_set` -> `member_key_script` over ssh | Yes: every container runs sshd on its published port |
| Role login per member (`lab_roles.user_for`) | `lab_roles.provision_users_script()`, run at VM provisioning | **No.** Containers don't run it. See below |
| Relay per member (its own nodes only) | `relay_command(topology, node_ports)`, one `vm-relay-topo` per agent | **Partly.** `relay_command` takes `targets` (done in #44), but the team attach path doesn't connect the relay to the lab network yet |
| `vmrun-<node>` wrappers, command guard per role | `vmrun_script(... deny=lr.denied_patterns(role))` | Yes: the wrapper runs ssh, and the guard runs in the wrapper |
| Team messages (`./peer-msg`) | `peer_msg_script`, `deliver_team_mail` | Yes, no node access involved |
| Key removal after a member's turn | `member_keys_set(remove=True)` | Yes |

## Design

1. **Lift the refusal.** `create_topo_run` stops refusing `team` when `build_mode == "full"`. Everything else in the
   team rules stays the same.

2. **Role logins on container nodes.** `lab-wire` (or a sibling `lab-roles` step run by `up()` after the links, as
   root through `docker exec`) runs `lab_roles.provision_users_script()` in every container node. The script only uses
   `useradd`, `sudo` files and `visudo`, all present in the image (`sudo` and `passwd` are in the base set). It runs
   once per container, after its links exist, same point as `lab-wire`.

3. **Member keys.** Unchanged. `member_keys_set` already uses ssh to the node's published port, which works for
   containers. The lab page's team attach therefore needs no change for keys.

4. **Relay for each member on the lab network.** The team attach path (the `relay_command(topology, {n: node_ports[n]
   for n in nodes})` call in `attach_agent_to_lab`'s team branch) gets the same two steps as the single-agent path
   added in #44: pass `targets=container_lab.relay_targets(rid, nodes)` and `connect_relay(rid, <relay id>)` after
   `compose up`. `detach_topo_agent` already removes the relay container, which drops its network connection.

5. **Forwarding for members.** A member can ask for `./forward <node> on|off` only for its own nodes. The request
   path from #43 already checks the lab's nodes; it has to check the member's nodes too. The 20-changes limit is per
   lab. Open question (see below): whether a member may change a router that belongs to another member.

6. **Refusals that stay.** Save/suspend, rollback, and the egress proxy stay refused for container labs. Nothing else
   about team labs changes.

## Trade-offs

- A container node has one kernel shared with the host. A member's command guard and sudo allowlist limit what a
  member can run, but a member with root in its container still has root there, like a VM member with its own login.
  This matches the VM design; it is not stronger.
- Role logins run in every container of the lab, so a role's sudo list is the same in a container as on a VM.
- No new host privileges: the root helpers (`link.sh`, `forward.sh`) are unchanged.

## Tests

- Unit: `create_topo_run` accepts `team` with `build_mode == "full"`; `provision_users_script` runs once per container
  in `up()` after the links; the team attach passes container relay targets and connects the relay.
- Forwarding: a member's request for a node outside its team is refused by `serve_forward_requests_once`.
- Live (Linux Docker host): two members, one host and one router, on the team's stages. Check: each member reaches only
  its own nodes; a role's refused command is refused; keys are removed after each turn.

## Open questions for review

1. Can a member switch forwarding on a router that another member owns, or only its own?
2. Should container role users get the same sudo lists as VM role users, or a narrower set?
3. One agent per lab stays the limit for container labs until 1 and 2 are answered?
