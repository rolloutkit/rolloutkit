// The same HTTP server as ../graceful, with the signal handling removed.
//
// http.ListenAndServe blocks in the main goroutine and nothing is watching for
// SIGTERM. This is the shape of a service written by someone who has not had to
// think about shutdown yet: correct under load, silent about termination.
package main

import (
	"log"
	"net/http"
	"time"
)

const workDuration = time.Second

func routes() *http.ServeMux {
	mux := http.NewServeMux()

	mux.HandleFunc("/ready", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ready"}`))
	})

	// Long enough to outlast Docker API signal-delivery latency on a loaded
	// runner, while still keeping the fixture cheap.
	mux.HandleFunc("/work", func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(workDuration)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"done"}`))
	})

	return mux
}

func main() {
	log.Println("listening on :8000 (no signal handling)")
	log.Fatal(http.ListenAndServe(":8000", routes()))
}
