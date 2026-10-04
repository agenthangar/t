FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl fzf gh git openssh-client procps python3 rsync \
        tmux uuid-runtime zsh \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/bin/zsh tester

COPY --chown=tester:tester source/ /opt/t-source/
COPY --chown=tester:tester playground/ /opt/playground/

RUN chmod +x /opt/playground/entrypoint.sh \
        /opt/playground/playground-install \
        /opt/playground/playground-demo-agent \
        /opt/playground/fake-claude \
    && ln -s /opt/playground/playground-install /usr/local/bin/playground-install \
    && ln -s /opt/playground/playground-demo-agent /usr/local/bin/playground-demo-agent

USER tester
ENV HOME=/home/tester \
    ZDOTDIR=/home/tester \
    XDG_CONFIG_HOME=/home/tester/.config \
    XDG_CACHE_HOME=/home/tester/.cache \
    XDG_DATA_HOME=/home/tester/.local/share \
    XDG_STATE_HOME=/home/tester/.local/state \
    TMUX_TMPDIR=/home/tester/.cache/tmux \
    TERM=xterm-256color
ENV PATH=/home/tester/bin:/opt/playground:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/tester
ENTRYPOINT ["/opt/playground/entrypoint.sh"]
CMD ["fresh"]
