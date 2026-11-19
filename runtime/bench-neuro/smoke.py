#!/usr/bin/env python3
"""bench-neuro:v1 构建期冒烟：主力包能 import，且能真算出结果。

「装上」≠「能用」：mne/nibabel 与 numpy ABI 不配、nilearn 缺 nibabel、wfdb 读不出都会
import 成功但一跑就炸。这里不只 import，还跑本方向题目真正用到的最小计算链：
  numpy 造信号 → scipy 滤波/周期图 → wfdb 往返写读一条本地 WFDB 记录
  → mne 用 numpy 建 RawArray 并取数据 → nibabel 往返写读一个 NIfTI → nilearn 掩膜取值
任一步抛异常则构建失败，避免「镜像建成了、判分时才发现用不了」。
"""
import os
import sys
import tempfile

FAIL = []


def check(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 冒烟就是要把任何异常都记下来
        FAIL.append("%s: %s: %s" % (name, type(e).__name__, e))


def t_scipy():
    import numpy as np
    from scipy import signal
    fs = 360.0
    x = np.sin(2 * np.pi * 5 * np.arange(0, 2, 1 / fs))
    b, a = signal.butter(4, 40 / (fs / 2), "low")
    y = signal.filtfilt(b, a, x)
    f, p = signal.periodogram(y, fs)
    assert abs(f[np.argmax(p)] - 5) < 1.0


def t_wfdb():
    import numpy as np
    import wfdb
    d = tempfile.mkdtemp()
    sig = np.c_[np.sin(np.linspace(0, 6, 720)), np.cos(np.linspace(0, 6, 720))]
    wfdb.wrsamp("rec", fs=360, units=["mV", "mV"], sig_name=["I", "II"],
                p_signal=sig, write_dir=d)
    rec = wfdb.rdrecord(os.path.join(d, "rec"))
    assert rec.p_signal.shape == (720, 2)


def t_mne():
    import numpy as np
    import mne
    info = mne.create_info(["C3", "C4"], 100.0, "eeg")
    raw = mne.io.RawArray(np.random.RandomState(0).randn(2, 500), info, verbose="ERROR")
    assert raw.get_data().shape == (2, 500)


def t_nibabel():
    import numpy as np
    import nibabel as nib
    d = tempfile.mkdtemp()
    img = nib.Nifti1Image(np.arange(27, dtype="float32").reshape(3, 3, 3), affine=np.eye(4))
    p = os.path.join(d, "x.nii.gz")
    nib.save(img, p)
    assert abs(float(nib.load(p).get_fdata().mean()) - 13.0) < 1e-6


def t_nilearn():
    import numpy as np
    import nibabel as nib
    from nilearn.masking import apply_mask
    d = tempfile.mkdtemp()
    data = np.ones((4, 4, 4, 2), dtype="float32")
    img = nib.Nifti1Image(data, np.eye(4))
    mask = nib.Nifti1Image(np.ones((4, 4, 4), dtype="uint8"), np.eye(4))
    out = apply_mask(img, mask)
    assert out.shape == (2, 64)


for n, f in [("scipy", t_scipy), ("wfdb", t_wfdb), ("mne", t_mne),
             ("nibabel", t_nibabel), ("nilearn", t_nilearn)]:
    check(n, f)

if FAIL:
    print("SMOKE FAIL:\n  " + "\n  ".join(FAIL), file=sys.stderr)
    sys.exit(1)
print("bench-neuro smoke OK: scipy/wfdb/mne/nibabel/nilearn 全部 import+计算通过")
