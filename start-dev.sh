#!/bin/bash
echo "Killing stale Postgres processes..."
taskkill //F //IM postgres.exe 2>/dev/null || true
taskkill //F //IM pg_ctl.exe 2>/dev/null || true
rm -f ~/.paperclip/instances/default/db/postmaster.pid
echo "Starting Paperclip dev server..."
pnpm dev
