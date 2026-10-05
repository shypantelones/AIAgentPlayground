# The image every container node of a "full" lab runs (see container_lab.py). One image for all roles: the role is
# given at start (AG_ROLE) and only changes what the start script does, not what is installed.
# Packages match a VM host's toolset, minus FRR (routing daemons are VM-router only) and minus ufw (containers have no
# internet to firewall; see container_lab.py).
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-server sudo iproute2 iputils-ping traceroute tcpdump dnsutils curl \
      iptables nftables bridge-utils nginx dnsmasq procps ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash bench \
 && echo 'bench ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/90-bench \
 && chmod 440 /etc/sudoers.d/90-bench \
 && ssh-keygen -A
COPY start.sh /usr/local/bin/lab-start
COPY wire.sh /usr/local/bin/lab-wire
RUN chmod +x /usr/local/bin/lab-start /usr/local/bin/lab-wire
EXPOSE 22
CMD ["/usr/local/bin/lab-start"]
