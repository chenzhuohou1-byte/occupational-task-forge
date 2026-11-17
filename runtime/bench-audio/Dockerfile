FROM ale-examinee:py3.12-sci-v1

COPY requirements.txt /opt/ale/audio-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: -r /opt/ale/audio-requirements.txt \
 && python -m pip check \
 && python -c 'import numpy as np, scipy, soundfile as sf; from io import BytesIO; b=BytesIO(); sf.write(b,np.zeros(1600),16000,format="FLAC"); b.seek(0); assert sf.read(b)[1] == 16000'

ENV OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
