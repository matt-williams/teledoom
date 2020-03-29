FROM eyeruleall/asterisk17
RUN apt-get update && apt-get install -y curl vim nodejs npm node-gyp nodejs-dev libssl1.0-dev
RUN npm install -g wscat
COPY etc/asterisk/* /etc/asterisk/
COPY var/lib/asterisk/sounds/* /var/lib/asterisk/sounds/

