ARG GO_IMAGE=golang:1.23.12-alpine3.22
ARG RUNTIME_IMAGE=alpine:3.22.1
FROM ${GO_IMAGE} AS build
WORKDIR /src
COPY go.mod *.go ./
RUN go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /esa-runtime .

FROM ${RUNTIME_IMAGE}
RUN apk add --no-cache ca-certificates && addgroup -S runtime && adduser -S -G runtime runtime
COPY --from=build /esa-runtime /usr/local/bin/esa-runtime
USER runtime
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/esa-runtime"]
