//go:build test

package cache

// SetWriteThroughFailForTesting enables forced WriteThrough failure in tests.
// The //go:build test constraint excludes this method from production builds;
// the writeThroughFail field on CachedCredentialGetter defaults to false and
// cannot be mutated without this setter.
func (c *CachedCredentialGetter) SetWriteThroughFailForTesting(fail bool) {
	c.writeThroughFail = fail
}
