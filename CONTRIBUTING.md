# Contributing to CouchLiteOS

Thanks for helping. Bug reports from real hardware are the most useful thing you
can send: CouchLiteOS runs on a wide range of old PCs, and every report helps.

## Reporting a bug

Open a [bug report](https://github.com/Dudiebug/couchliteos/issues/new?template=bug_report.yml).
It asks for the CouchLiteOS version (shown in Settings > SOFTWARE UPDATE), your PC and graphics card, what
you did and what happened. Attach a support file if you can: plug in a USB drive and choose
Settings > GENERATE SUPPORT FILE. The
[wiki](https://github.com/Dudiebug/couchliteos/wiki/Support-File-and-Privacy) explains what
the file contains; passwords and keys are left out.

Questions and ideas go in a
[feature request](https://github.com/Dudiebug/couchliteos/issues/new?template=feature_request.yml).
Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Working on the code

[docs/BUILDING.md](docs/BUILDING.md) covers the build host, test and release builds, the
caches, and the tests. Before opening a pull request:

- `make test` passes (it needs `python3` and `ripgrep`).
- A change to how the box looks or behaves comes with a test.
- User-facing text uses the names the screen shows (for example Settings > CONTROLS).
- Pull requests target the current `release/X.Y.Z` branch, not `main`.

Commit messages start with a short summary line in the imperative ("Fix ...", "Add ..."),
followed by a blank line and what changed and why, when that is not obvious.

## Versions and releases

- Versions are `MAJOR.MINOR.PATCH`. A patch release only fixes things; a minor release
  adds features.
- Work happens on a `release/X.Y.Z` branch. `main` always matches the latest release.
- Releases are tagged `vX.Y.Z` and include the ISOs, `SHA256SUMS`, and release notes
  written for people using the box (`docs/releases/vX.Y.Z.md`).
- Only the latest release gets fixes. Boxes on 0.2.1 or newer update from
  Settings > SOFTWARE UPDATE; older ones are reinstalled once.
- `make release-check` must pass before a release is published.

## License

CouchLiteOS is licensed under the [GNU GPL v3](LICENSE). By contributing you agree that your
contribution is licensed under it too. Third-party components are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
