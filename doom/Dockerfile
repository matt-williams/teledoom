# TODO: Multi-stage build to get rid of build dependencies
FROM python:3.8-slim
RUN apt-get update && apt-get install -y build-essential cmake ffmpeg git libboost-all-dev libsdl2-dev make unzip wget && apt-get clean
RUN pip install --no-cache-dir ffmpeg-python==0.2.0 pillow==7.0.0 phonenumbers==8.12.0 vizdoom==1.1.7 git+git://github.com/matt-williams/aripy3.git@cce1d36#egg=aripy3

COPY teledoom.py /
COPY overlay.png /
CMD ["python3", "/teledoom.py"]
