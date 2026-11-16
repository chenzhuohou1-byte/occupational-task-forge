ARG BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple/
COPY requirements.txt /opt/ale/qiskit-requirements.txt
RUN python -m venv /opt/ale/qiskit \
 && /opt/ale/qiskit/bin/python -m pip install --index-url "$PIP_INDEX_URL" --no-cache-dir --no-deps --only-binary=:all: -r /opt/ale/qiskit-requirements.txt \
 && /opt/ale/qiskit/bin/python -m pip check \
 && /opt/ale/qiskit/bin/python -c 'from qiskit import QuantumCircuit; from qiskit.quantum_info import Statevector; assert Statevector.from_instruction(QuantumCircuit(1)).probabilities()[0] == 1'

ENV PATH="/opt/ale/qiskit/bin:${PATH}" OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
