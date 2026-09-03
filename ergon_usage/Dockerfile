ARG BUILD_FROM
FROM $BUILD_FROM

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# Ensure Chromium and its system dependencies are present.
RUN python -m playwright install --with-deps chromium

COPY app/ /opt/ergon_usage/app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PYTHONPATH=/opt/ergon_usage

CMD ["/run.sh"]
