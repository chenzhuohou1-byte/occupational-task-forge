FROM docker.m.daocloud.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
COPY requirements.txt /opt/ale/stim-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: -r /opt/ale/stim-requirements.txt \
 && python -m pip check \
 && python -c 'import stim; assert str(stim.PauliString("X").after(stim.Circuit("H 0"))) == "+Z"'
ENV OPENBLAS_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
