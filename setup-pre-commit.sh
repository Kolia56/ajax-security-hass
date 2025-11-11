#!/bin/bash
# Setup pre-commit hooks for Ajax integration

echo "Installing pre-commit..."

# Check if pre-commit is installed
if ! command -v pre-commit &> /dev/null; then
    echo "pre-commit not found. Installing via pacman..."
    sudo pacman -S python-pre-commit --noconfirm
fi

# Install the git hooks
echo "Installing git hooks..."
pre-commit install

echo ""
echo "✅ Pre-commit hooks installed successfully!"
echo ""
echo "Now, every time you commit, ruff will automatically:"
echo "  - Check your code for issues"
echo "  - Format your code"
echo "  - Only commit if everything passes"
echo ""
echo "To run manually: pre-commit run --all-files"
