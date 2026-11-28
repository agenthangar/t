FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      bash ca-certificates gcc libc6-dev fzf git openssh-client openssh-server \
      procps python3 rsync tmux zsh && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_*
RUN mkdir /run/sshd
CMD ["sh", "-ec", "ssh-keygen -A >/dev/null && exec /usr/sbin/sshd -D -e"]
