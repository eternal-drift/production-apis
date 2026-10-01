#!/usr/bin/env bash
# Regenerate protobuf/gRPC stubs from proto/ into src/production_apis/gen/.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=src/production_apis/gen
rm -rf "$OUT/orders"
uv run python -m grpc_tools.protoc -I proto \
  --python_out="$OUT" --pyi_out="$OUT" --grpc_python_out="$OUT" \
  proto/orders/v1/orders.proto
touch "$OUT/orders/__init__.py" "$OUT/orders/v1/__init__.py"
# protoc emits absolute imports rooted at the proto path; re-root them inside our package.
sed -i.bak 's/^from orders\.v1 import/from production_apis.gen.orders.v1 import/' "$OUT/orders/v1/orders_pb2_grpc.py"
rm -f "$OUT"/orders/v1/*.bak
echo "generated $OUT/orders/v1"
