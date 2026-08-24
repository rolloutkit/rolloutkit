FROM golang:1.23-alpine AS build

WORKDIR /src
COPY go.mod main.go ./
# CGO_ENABLED=0 for a statically linked binary: scratch has no libc to link
# against, and no shell either — which is the point. The application is PID 1
# with nothing between it and the signal.
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/server .

FROM scratch
COPY --from=build /out/server /server
EXPOSE 8000
# Exec-form, single binary: no shell to swallow or forward anything.
ENTRYPOINT ["/server"]
