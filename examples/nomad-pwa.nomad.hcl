# Example Nomad job for the front end: stock nginx serving the static PWA from a
# shared directory and proxying /api to the bridge. Adjust paths, host and
# Traefik router to taste.
job "meshcore-pwa" {
  datacenters = ["dc1"]
  type        = "service"

  group "meshcore-pwa" {
    network {
      mode = "bridge"
      port "http" { to = 80 }
    }

    task "nginx" {
      driver = "docker"

      config {
        image   = "nginx:1.27-alpine"
        ports   = ["http"]
        volumes = [
          "/srv/meshcore-pwa:/usr/share/nginx/html:ro",                       # PWA_DEST
          "local/default.conf.template:/etc/nginx/templates/default.conf.template:ro",
        ]
      }

      env { BRIDGE_URL = "http://192.0.2.10:8190" }

      template {
        destination     = "local/default.conf.template"
        left_delimiter  = "[["
        right_delimiter = "]]"
        data            = <<-EOT
          server {
            listen 80;
            root /usr/share/nginx/html;
            location /api/ {
              proxy_pass ${BRIDGE_URL};
              proxy_http_version 1.1;
              proxy_set_header Connection "";
              proxy_buffering off;
              proxy_read_timeout 1h;
            }
            location = /sw.js { add_header Cache-Control "no-cache"; try_files $uri =404; }
            location / { add_header Cache-Control "no-cache"; try_files $uri /index.html; }
          }
        EOT
      }

      resources {
        cpu    = 50
        memory = 64
      }

      service {
        name     = "meshcore-pwa"
        port     = "http"
        provider = "consul"
        tags = [
          "traefik.enable=true",
          "traefik.http.routers.meshcore-pwa.rule=Host(`mesh.example.lan`)",
          "traefik.http.routers.meshcore-pwa.tls=true",
        ]
      }
    }
  }
}
