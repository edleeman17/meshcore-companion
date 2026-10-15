FROM nginx:1.27-alpine
COPY static/ /usr/share/nginx/html/
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
ENV BRIDGE_URL=http://host.docker.internal:8190
EXPOSE 80
