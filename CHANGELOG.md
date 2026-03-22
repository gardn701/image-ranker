# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-03-22

### Added

- Packaging metadata and editable-install support via `pyproject.toml`.
- A reproducible pytest setup for local development and CI.
- GitHub Actions workflows for automated tests and tag-driven GitHub releases.
- A release-notes extractor so GitHub releases publish the matching changelog section.

### Changed

- Added the `image-ranker` console entry point for launching the Flask app.
- Standardized path handling around `BASE_DIR` so app configuration and tests behave consistently.
- Marked the first formal stable release as `1.0.0`.
