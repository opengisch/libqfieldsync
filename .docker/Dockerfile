ARG QGIS_TEST_VERSION=latest
FROM  qgis/qgis:${QGIS_TEST_VERSION}

RUN apt-get update \
    && apt-get install -y python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY ./requirements.txt /tmp/

RUN pip3 install -r /tmp/requirements.txt --break-system-packages

ENV LANG=C.UTF-8

WORKDIR /usr/src
