# The box a work package is judged in.
#
# Everything the run needs is baked in, because the container has no network:
# `--network=none` is what stops corpus case 7 fetching the upstream fix, and it
# also means nothing can be installed once the run starts. A package that needs
# a dependency we do not ship cannot be verified, and that is the correct
# failure — a verifier that reaches the internet to satisfy a run is measuring
# search rather than work.
FROM python:3.12-alpine

# Pinned. A floating version means the box a verdict was reached in is not the
# box that reproduces it, and a verdict nobody can reproduce is not evidence.
RUN pip install --no-cache-dir pytest==8.3.4

WORKDIR /work

# Not root, in the IMAGE as well as on the `docker run` command line.
#
# StackIsolationTest caught this: the first version relied on `--user nobody`
# at run time, and its own words are the reason — "a runtime image running as
# root gives an RCE the host's capability set to work with". A flag on a command
# line is a flag someone can forget, and this is the one container in the
# product that deliberately runs code written by anonymous strangers.
USER nobody
