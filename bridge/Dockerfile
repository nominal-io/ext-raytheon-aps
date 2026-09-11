FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    nominal \
    websockets \
    grpcio \
    grpcio-tools \
    numpy \
    pillow \
    requests

COPY squall_rf.proto squall_ir.proto /app/
RUN python -m grpc_tools.protoc -I/app \
    --python_out=/app --grpc_python_out=/app \
    squall_rf.proto squall_ir.proto

COPY bridge.py /app/

ENTRYPOINT ["python", "-u", "/app/bridge.py"]
