#!/bin/bash
# Bump semantic version based on type
# Usage: ./bump-version.sh <current_version> <bump_type>
# Example: ./bump-version.sh v1.2.3 patch

set -e

CURRENT_VERSION=$1
BUMP_TYPE=$2

# Validate inputs
if [ -z "$CURRENT_VERSION" ] || [ -z "$BUMP_TYPE" ]; then
    echo "Error: Missing required arguments"
    echo "Usage: $0 <current_version> <bump_type>"
    echo "Example: $0 v1.2.3 patch"
    exit 1
fi

# Validate bump type
if [[ ! "$BUMP_TYPE" =~ ^(major|minor|patch)$ ]]; then
    echo "Error: Invalid bump type '$BUMP_TYPE'"
    echo "Valid types: major, minor, patch"
    exit 1
fi

# Strip 'v' prefix if present
VERSION=${CURRENT_VERSION#v}

# Validate version format
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: Invalid version format '$VERSION'"
    echo "Expected format: X.Y.Z (e.g., 1.2.3)"
    exit 1
fi

# Split into components
IFS='.' read -r -a VERSION_PARTS <<< "$VERSION"
MAJOR=${VERSION_PARTS[0]}
MINOR=${VERSION_PARTS[1]}
PATCH=${VERSION_PARTS[2]}

# Bump based on type
case $BUMP_TYPE in
  major)
    MAJOR=$((MAJOR + 1))
    MINOR=0
    PATCH=0
    ;;
  minor)
    MINOR=$((MINOR + 1))
    PATCH=0
    ;;
  patch)
    PATCH=$((PATCH + 1))
    ;;
esac

# Construct new version
NEW_VERSION="v${MAJOR}.${MINOR}.${PATCH}"

# Output new version (for GitHub Actions to capture)
echo "$NEW_VERSION"

# Log the bump to stderr so it doesn't interfere with stdout capture
echo "Bumped $CURRENT_VERSION → $NEW_VERSION ($BUMP_TYPE)" >&2
