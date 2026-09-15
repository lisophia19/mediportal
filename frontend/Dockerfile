# Production image: build the static bundle, then serve it with nginx --
# Vite's dev server (`npm run dev`) rejects requests from hosts it does not
# recognize (e.g. a nip.io hostname) and is not meant to be deployed anyway.
FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
# Vite bakes VITE_* vars into the bundle at build time, not read at
# container start -- must be a build arg, not a runtime environment var.
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
