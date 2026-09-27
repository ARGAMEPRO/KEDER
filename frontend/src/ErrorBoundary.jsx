import { Component } from 'react'

export default class ErrorBoundary extends Component {
  state = { err: null }
  static getDerivedStateFromError(err) { return { err } }
  componentDidCatch(err, info) { console.error('[kedr]', err, info) }
  render() {
    if (!this.state.err) return this.props.children
    return (
      <div role="alert" className="m-4 max-w-md space-y-3 rounded-2xl border border-border/50 bg-card p-6">
        <p className="font-semibold">{this.props.title}</p>
        <button className="min-h-11 cursor-pointer rounded-full bg-primary px-5 text-sm font-semibold text-primary-foreground" onClick={() => this.setState({ err: null })}>{this.props.retry}</button>
      </div>
    )
  }
}
