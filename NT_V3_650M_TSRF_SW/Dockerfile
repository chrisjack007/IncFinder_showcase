FROM quay.io/condaforge/miniforge3@sha256:e41989536043cfed46b377c1b80622741ea91e03314e36119e0225f7cf1eb731
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false CUDA_VISIBLE_DEVICES=-1 HF_HOME=/tmp/huggingface \
    INCFINDER_MODEL_DIR=/models
COPY environments/linux-64.lock /tmp/linux-64.lock
RUN CONDA_OVERRIDE_CUDA='' conda create --yes --name IncFinder_Test1 --file /tmp/linux-64.lock \
    && conda clean --all --yes
COPY dist/channel /tmp/incfinder-channel
RUN CONDA_OVERRIDE_CUDA='' conda install --yes --offline --name IncFinder_Test1 \
    /tmp/incfinder-channel/noarch/incfinder-0.5.0rc2-py_0.conda \
    && conda clean --all --yes && rm -rf /tmp/incfinder-channel
WORKDIR /work
ENTRYPOINT ["conda", "run", "--no-capture-output", "-n", "IncFinder_Test1", "incfinder"]
CMD ["--help"]
