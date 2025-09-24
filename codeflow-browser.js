// Browser-compatible version of CodeFlow
(function() {
  'use strict';
  
  const { useState, useRef, useCallback, useMemo, useEffect } = React;
  const { createElement: h } = React;

  // Simple CodeFlow component for browser
  const CodeFlowPlatform = () => {
    const [code, setCode] = useState('');
    const [analysis, setAnalysis] = useState(null);
    const [loading, setLoading] = useState(false);
    
    const analyzeCode = useCallback(() => {
      if (!code.trim()) return;
      
      setLoading(true);
      
      // Simple analysis simulation
      setTimeout(() => {
        const lines = code.split('\n').length;
        const chars = code.length;
        const words = code.split(/\s+/).filter(w => w.length > 0).length;
        
        setAnalysis({
          complexity: Math.floor(Math.random() * 10) + 1,
          security: Math.floor(Math.random() * 5) + 1,
          maintainability: Math.floor(Math.random() * 10) + 1,
          lines,
          characters: chars,
          words,
          issues: [
            'Consider using const instead of var',
            'Function complexity could be reduced',
            'Add error handling for async operations'
          ]
        });
        setLoading(false);
      }, 1500);
    }, [code]);

    const handleFileUpload = (event) => {
      const file = event.target.files[0];
      if (file) {
        const reader = new FileReader();
        reader.onload = (e) => {
          setCode(e.target.result);
        };
        reader.readAsText(file);
      }
    };

    return h('div', {
      style: {
        minHeight: '100vh',
        background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
        padding: '2rem',
        fontFamily: 'system-ui, sans-serif'
      }
    }, [
      // Header
      h('header', {
        key: 'header',
        style: {
          textAlign: 'center',
          marginBottom: '2rem',
          color: 'white'
        }
      }, [
        h('h1', {
          key: 'title',
          style: { fontSize: '3rem', margin: '0 0 1rem 0' }
        }, '🧠 CodeFlow'),
        h('p', {
          key: 'subtitle',
          style: { fontSize: '1.2rem', opacity: 0.9 }
        }, 'AI-Powered Code Analysis Platform')
      ]),

      // Main content
      h('main', {
        key: 'main',
        style: {
          maxWidth: '1200px',
          margin: '0 auto',
          background: 'rgba(255,255,255,0.95)',
          borderRadius: '12px',
          padding: '2rem',
          boxShadow: '0 20px 40px rgba(0,0,0,0.1)'
        }
      }, [
        // File upload section
        h('div', {
          key: 'upload-section',
          style: { marginBottom: '2rem' }
        }, [
          h('label', {
            key: 'upload-label',
            style: {
              display: 'block',
              marginBottom: '1rem',
              fontSize: '1.1rem',
              fontWeight: 'bold',
              color: '#333'
            }
          }, '📤 Upload Code File or Paste Code:'),
          h('input', {
            key: 'file-input',
            type: 'file',
            accept: '.js,.jsx,.ts,.tsx,.py,.java,.cpp,.c,.php,.rb,.go,.rs',
            onChange: handleFileUpload,
            style: {
              marginBottom: '1rem',
              padding: '0.5rem',
              border: '2px dashed #667eea',
              borderRadius: '8px',
              width: '100%'
            }
          })
        ]),

        // Code textarea
        h('div', {
          key: 'code-section',
          style: { marginBottom: '2rem' }
        }, [
          h('textarea', {
            key: 'code-input',
            value: code,
            onChange: (e) => setCode(e.target.value),
            placeholder: 'Paste your code here or upload a file...',
            style: {
              width: '100%',
              height: '300px',
              padding: '1rem',
              border: '2px solid #e1e5e9',
              borderRadius: '8px',
              fontFamily: 'Monaco, Consolas, monospace',
              fontSize: '14px',
              resize: 'vertical'
            }
          })
        ]),

        // Analyze button
        h('div', {
          key: 'button-section',
          style: { textAlign: 'center', marginBottom: '2rem' }
        }, [
          h('button', {
            key: 'analyze-btn',
            onClick: analyzeCode,
            disabled: loading || !code.trim(),
            style: {
              background: loading ? '#ccc' : 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
              color: 'white',
              border: 'none',
              padding: '1rem 2rem',
              fontSize: '1.1rem',
              borderRadius: '8px',
              cursor: loading ? 'not-allowed' : 'pointer',
              transition: 'all 0.3s ease'
            }
          }, loading ? '🔄 Analyzing...' : '🔍 Analyze Code')
        ]),

        // Results section
        analysis && h('div', {
          key: 'results',
          style: {
            background: '#f8f9fa',
            padding: '2rem',
            borderRadius: '8px',
            border: '1px solid #e9ecef'
          }
        }, [
          h('h3', {
            key: 'results-title',
            style: { color: '#333', marginTop: 0 }
          }, '📊 Analysis Results'),

          // Metrics grid
          h('div', {
            key: 'metrics',
            style: {
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: '1rem',
              marginBottom: '2rem'
            }
          }, [
            h('div', {
              key: 'complexity',
              style: {
                background: 'white',
                padding: '1rem',
                borderRadius: '8px',
                textAlign: 'center',
                border: '1px solid #e9ecef'
              }
            }, [
              h('div', { key: 'complexity-label', style: { fontSize: '0.9rem', color: '#666' }}, 'Complexity'),
              h('div', { key: 'complexity-value', style: { fontSize: '2rem', fontWeight: 'bold', color: analysis.complexity > 7 ? '#dc3545' : analysis.complexity > 4 ? '#ffc107' : '#28a745' }}, analysis.complexity + '/10')
            ]),

            h('div', {
              key: 'security',
              style: {
                background: 'white',
                padding: '1rem',
                borderRadius: '8px',
                textAlign: 'center',
                border: '1px solid #e9ecef'
              }
            }, [
              h('div', { key: 'security-label', style: { fontSize: '0.9rem', color: '#666' }}, 'Security Score'),
              h('div', { key: 'security-value', style: { fontSize: '2rem', fontWeight: 'bold', color: analysis.security > 3 ? '#28a745' : analysis.security > 1 ? '#ffc107' : '#dc3545' }}, analysis.security + '/5')
            ]),

            h('div', {
              key: 'maintainability',
              style: {
                background: 'white',
                padding: '1rem',
                borderRadius: '8px',
                textAlign: 'center',
                border: '1px solid #e9ecef'
              }
            }, [
              h('div', { key: 'maintain-label', style: { fontSize: '0.9rem', color: '#666' }}, 'Maintainability'),
              h('div', { key: 'maintain-value', style: { fontSize: '2rem', fontWeight: 'bold', color: analysis.maintainability > 7 ? '#28a745' : analysis.maintainability > 4 ? '#ffc107' : '#dc3545' }}, analysis.maintainability + '/10')
            ]),

            h('div', {
              key: 'stats',
              style: {
                background: 'white',
                padding: '1rem',
                borderRadius: '8px',
                textAlign: 'center',
                border: '1px solid #e9ecef'
              }
            }, [
              h('div', { key: 'stats-label', style: { fontSize: '0.9rem', color: '#666' }}, 'Code Stats'),
              h('div', { key: 'stats-value', style: { fontSize: '1rem', fontWeight: 'bold', color: '#333' }}, `${analysis.lines} lines, ${analysis.words} words`)
            ])
          ]),

          // Issues
          h('div', { key: 'issues-section' }, [
            h('h4', { key: 'issues-title', style: { color: '#333' }}, '⚠️ Suggestions:'),
            h('ul', { key: 'issues-list', style: { color: '#666' }}, 
              analysis.issues.map((issue, index) => 
                h('li', { key: index, style: { marginBottom: '0.5rem' }}, issue)
              )
            )
          ])
        ])
      ]),

      // Footer
      h('footer', {
        key: 'footer',
        style: {
          textAlign: 'center',
          marginTop: '3rem',
          color: 'white',
          opacity: 0.8
        }
      }, [
        h('p', { key: 'footer-text' }, '© 2024 CodeFlow - Powered by AI 🚀')
      ])
    ]);
  };

  // Render the app
  const root = ReactDOM.createRoot(document.getElementById('root'));
  root.render(h(CodeFlowPlatform));
})();