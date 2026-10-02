# Third-party software

This project (MIT, see [LICENSE](LICENSE)) is orchestration code: Python, Docker Compose files, proxy/relay configs and
scripts. It does **not** include or redistribute the software below. It downloads or starts it on your machine, at
install or run time, under each project's own license. Those licenses are not changed by this project's license, and you
are responsible for complying with them.

| Software | How this project uses it | License |
|---|---|---|
| [OpenClaw](https://github.com/openclaw/openclaw) | the agent software, pulled as the `ghcr.io/openclaw/openclaw` container image | MIT (checked in its repository) |
| [Ollama](https://ollama.com) | local model server (`ollama/ollama` image, or a native install) | believed MIT, verify |
| Language models you download (e.g. `qwen3`) | run by Ollama | each model has its **own license and terms**: check them on the model's page before use |
| [Squid](http://www.squid-cache.org) | egress allowlist proxy (`ubuntu/squid` image) | believed GPL-2.0-or-later, verify |
| [socat](http://www.dest-unreach.org/socat/) | one-port forwarders (`alpine/socat` image) | believed GPL-2.0, verify |
| [nginx](https://nginx.org) | credential relay for cloud mode (`nginxinc/nginx-unprivileged` image) | believed BSD-2-Clause, verify |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) / Docker Engine | container runtime | Docker Desktop has its own subscription terms (free for personal use and small businesses; paid for larger companies); Docker Engine is Apache-2.0 |
| [VirtualBox](https://www.virtualbox.org), [Vagrant](https://www.vagrantup.com) | optional VM tier only | VirtualBox believed GPL-3.0 (its Extension Pack has separate terms); Vagrant believed BUSL-1.1, verify |
| Python | runs the control panel (standard library only) | PSF License |

"Believed" means the author did not verify the license text while writing this file. Check each project before relying on it,
particularly if you redistribute container images or use this commercially.

## Cloud model providers
If you enable cloud mode you send prompts to a provider you choose (Anthropic, OpenAI or another). Their terms of service and
pricing apply to your use, and you are responsible for your own API keys and spend.
