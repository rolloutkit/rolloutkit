// The same HTTP server as ../nosignal, plus the twelve lines that make it
// shut down properly: catch SIGTERM, stop accepting, let what is in flight
// finish, then leave.
//
// srv.Shutdown is the interesting part. It closes idle keep-alive connections
// immediately and lets active requests run to completion, which is exactly the
// behaviour SP005 is written to detect the absence of.
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const workDuration = 50 * time.Millisecond

// Generous on purpose: the point of this fixture is that shutdown finishes
// long before any deadline, not that it races one.
const shutdownTimeout = 20 * time.Second

func routes() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ready"}`))
	})

	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(workDuration)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"done"}`))
	})

	return mux
}

func main() {
	srv := &http.Server{Addr: ":8000", Handler: routes()}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGTERM, syscall.SIGINT)

	go func() {
		log.Println("listening on :8000 (graceful shutdown)")
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("listen: %v", err)
		}
	}()

	sig := <-stop
	log.Printf("received %v, shutting down", sig)

	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("shutdown: %v", err)
		os.Exit(1)
	}
	log.Println("shutdown complete")
}
