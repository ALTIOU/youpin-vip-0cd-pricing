# SPDX-License-Identifier: AGPL-3.0-only
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
# The release must include these notices; added by the provenance review.
COPY LICENSE COPYING.GPL-3.0 THIRD_PARTY_NOTICES.txt LICENSING.txt ./
COPY LICENSES ./LICENSES
RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["youpin-reprice"]
CMD ["--state", "/state/session.json", "--policy", "/config/policy.json", "--output", "/state/latest-result.json", "--loop"]
