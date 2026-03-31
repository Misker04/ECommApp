#!/bin/bash

python3 -m grpc_tools.protoc \
  -I./src/proto \
  --python_out=./src/proto \
  --grpc_python_out=./src/proto \
  src/proto/customer.proto \
  src/proto/product.proto

# works on macOS, Linux, and Windows (Git Bash/WSL)
if [[ "$OSTYPE" == "darwin"* ]]; then
  # macOS
  sed -i '' 's/import customer_pb2/from src.proto import customer_pb2/g' src/proto/customer_pb2_grpc.py
  sed -i '' 's/import product_pb2/from src.proto import product_pb2/g' src/proto/product_pb2_grpc.py
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
  # Windows (Git Bash / Cygwin)
  python3 -c "
import pathlib
for path, old, new in [
    ('src/proto/customer_pb2_grpc.py', 'import customer_pb2', 'from src.proto import customer_pb2'),
    ('src/proto/product_pb2_grpc.py',  'import product_pb2',  'from src.proto import product_pb2'),
]:
    p = pathlib.Path(path)
    p.write_text(p.read_text().replace(old, new))
"
else
  # Linux
  sed -i 's/import customer_pb2/from src.proto import customer_pb2/g' src/proto/customer_pb2_grpc.py
  sed -i 's/import product_pb2/from src.proto import product_pb2/g' src/proto/product_pb2_grpc.py
fi

echo "Proto stubs regenerated and imports fixed."