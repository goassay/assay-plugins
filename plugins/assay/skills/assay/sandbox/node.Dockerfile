# The box a JavaScript work package is judged in (E22 Part 2).
#
# Nothing installed: Node 22's own test runner writes the JUnit report the
# verdict is read from (`node --test --test-reporter=junit`), so the box stays
# without a network and without a package manager's reach. A package that
# needs a dependency this image does not ship cannot be verified, and that is
# the correct failure, as it is for the Python box.
FROM node:22-alpine

WORKDIR /work

# Not root, in the IMAGE as well as on the command line — the same reasoning
# as verifier/Dockerfile: this container runs code written by strangers.
USER nobody
